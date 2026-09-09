"""Device wrappers: advertisement state + optional Meross GATT control."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    clear_cache,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_RETRY_COUNT,
    GATT_ADV_WAIT_TIMEOUT,
    GATT_FRESH_ADV_SECONDS,
    GATT_INPROGRESS_COOLDOWN,
    GATT_NOTIFY_TIMEOUT,
    GATT_POST_CONNECT_SETTLE,
    GATT_REDISCOVER_SETTLE,
    HISTORY_PAGE_SIZE,
    MEROSS_CHAR_NOTIFY,
    MEROSS_CHAR_WRITE,
    MEROSS_GATT_SERVICE,
    MODEL_TO_SUBDEV,
    TAG_HUMI_HISTORY_COUNT,
    TAG_HUMI_HISTORY_DATA,
    TAG_TEMP_HISTORY_COUNT,
    TAG_TEMP_HISTORY_DATA,
    MerossModel,
    ms700_logical_button,
)
from .gatt import MerossBleGattGate
from .parser import MerossAdvertisement
from .protocol import (
    HistorySample,
    build_heartbeat_frame,
    build_humi_history_count_frame,
    build_humi_history_data_frame,
    build_identify_frame,
    build_temp_history_count_frame,
    build_temp_history_data_frame,
    iter_tlvs,
    parse_ack_success,
    parse_history_count,
    parse_history_samples,
)

_LOGGER = logging.getLogger(__name__)

_LOG_SVC = "[SVC-DISCOVER]"
_LOG_SVC_FAIL = "[SVC-DISCOVER-FAIL]"
_LOG_NOTIFY = "[START-NOTIFY]"
_LOG_CACHE = "[CACHE]"
_LOG_TIMEOUT = "[超时----]"
_LOG_SETTLE = "[连接稳定]"
_LOG_CLEAR = "[清缓存]"
_LOG_REDISCOVER = "[补发现]"


def _short_repr(value: Any, limit: int = 400) -> str:
    text = repr(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _uuid_key(value: object) -> str:
    return str(value).lower().replace("-", "")


def _char_matches(char: Any, expected: str) -> bool:
    return _uuid_key(getattr(char, "uuid", char)) == _uuid_key(expected)


class MerossBLEError(Exception):
    """Device-layer error."""


class MerossBLEHistoryYield(MerossBLEError):
    """History aborted so Identify can use the shared GATT slot."""


class MerossBLENotifyTimeout(MerossBLEError):
    """Connected, but firmware did not ACK on the notify characteristic."""


class MerossBLEDevice:
    """Base device updated from advertisements."""

    def __init__(
        self,
        device: BLEDevice,
        model: MerossModel,
        retry_count: int = DEFAULT_RETRY_COUNT,
    ) -> None:
        self._device = device
        self.model = model
        self.retry_count = retry_count
        self._data: dict[str, Any] = {}
        self._last_adv: MerossAdvertisement | None = None
        self._msg_id = 0
        # Instantaneous report_reqId: accept each id once (rebroadcasts keep same id).
        # Do not clear on idle ads — firmware may briefly idle then rebroadcast the
        # same press, which previously caused duplicate HA events / "stuck" presses.
        self._last_accepted_req_id: int | None = None
        self._last_seen_req_id: int | None = None
        self._stale_event_logged_for: int | None = None
        # First event-carrying adv after process start is often a sticky
        # rebroadcast from before HA restarted — sync req_id, do not fire.
        self._event_req_id_bootstrapped = False
        self._hass: HomeAssistant | None = None
        self._connectable = True
        self._gatt_gate: MerossBleGattGate | None = None
        self._wait_advertisement: Callable[[float], Awaitable[bool]] | None = None
        self._last_adv_monotonic: float | None = None
        # BlueZ RemoveDevice at most once per GATT operation (identify/history).
        self._bluez_remove_done = False

    def bind_runtime(
        self,
        hass: HomeAssistant,
        *,
        connectable: bool,
        gatt_gate: MerossBleGattGate,
        wait_advertisement: Callable[[float], Awaitable[bool]],
    ) -> None:
        """Attach HA runtime helpers used for GATT (shared slot + adv window)."""
        self._hass = hass
        self._connectable = connectable
        self._gatt_gate = gatt_gate
        self._wait_advertisement = wait_advertisement

    def _raise_if_history_should_yield(self) -> None:
        if self._gatt_gate is not None and self._gatt_gate.identify_waiting:
            raise MerossBLEHistoryYield(
                "Identify waiting for GATT; history yielding"
            )

    @property
    def address(self) -> str:
        return self._device.address

    @property
    def name(self) -> str:
        return self._device.name or self.address

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def parsed_data(self) -> dict[str, Any]:
        return self._data

    @property
    def subdev_type(self) -> int:
        return MODEL_TO_SUBDEV.get(self.model, 0)

    def update_ble_device(self, device: BLEDevice) -> None:
        self._device = device

    def _async_refresh_ble_device(self) -> None:
        """Use the most recently heard BLEDevice (macOS often flags ads non-connectable)."""
        if self._hass is None:
            return
        info = bluetooth.async_last_service_info(
            self._hass, self.address, connectable=False
        )
        if info is not None:
            self._device = info.device
            return
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self.address.upper(), False
        )
        if ble_device is not None:
            self._device = ble_device

    @staticmethod
    def _is_slot_or_inprogress_error(err: BaseException) -> bool:
        text = str(err)
        return (
            "InProgress" in text
            or "connection slot" in text.lower()
            or "out of connection slots" in text.lower()
        )

    def _log_ha_ble_cache(self, reason: str) -> None:
        """Dump HA bluetooth-manager cache and the BLEDevice about to be used."""
        device = self._device
        _LOGGER.debug(
            "%s %s %s using BLEDevice name=%r rssi=%s details=%s",
            self.address,
            _LOG_CACHE,
            reason,
            device.name,
            getattr(device, "rssi", None),
            _short_repr(getattr(device, "details", None)),
        )
        if self._hass is None:
            _LOGGER.debug(
                "%s %s HA manager unavailable (no hass; bind path)",
                self.address,
                _LOG_CACHE,
            )
            return
        for connectable in (True, False):
            info = bluetooth.async_last_service_info(
                self._hass, self.address, connectable=connectable
            )
            ble = bluetooth.async_ble_device_from_address(
                self._hass, self.address.upper(), connectable
            )
            if info is None:
                _LOGGER.debug(
                    "%s %s HA last_service_info connectable=%s empty "
                    "ble_device=%s",
                    self.address,
                    _LOG_CACHE,
                    connectable,
                    _short_repr(ble),
                )
                continue
            age = time.monotonic() - info.time
            adv = info.advertisement
            service_data = {
                str(key): bytes(value).hex()
                for key, value in (adv.service_data or {}).items()
            }
            _LOGGER.debug(
                "%s %s HA last_service_info connectable=%s age=%.1fs "
                "name=%r rssi=%s adv_connectable=%s service_uuids=%s "
                "service_data=%s ble_device_name=%r",
                self.address,
                _LOG_CACHE,
                connectable,
                age,
                info.name,
                info.rssi,
                info.connectable,
                list(adv.service_uuids or []),
                service_data or "(none)",
                None if ble is None else ble.name,
            )

    def _log_gatt_discovery(self, client: BleakClientWithServiceCache) -> bool:
        """Log GATT services after connect; return True if Meross chars are present."""
        cached = getattr(client, "_cached_services", None)
        try:
            services = client.services
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s %s cannot read services cached=%s: %s",
                self.address,
                _LOG_SVC_FAIL,
                cached is not None,
                err,
            )
            return False
        if not services:
            _LOGGER.debug(
                "%s %s empty services (cache_hit=%s client=%s)",
                self.address,
                _LOG_SVC_FAIL,
                cached is not None,
                type(client).__name__,
            )
            return False

        has_service = False
        has_write = False
        has_notify = False
        chars: list[str] = []
        service_count = 0
        for service in services:
            service_count += 1
            if _uuid_key(service.uuid) == _uuid_key(MEROSS_GATT_SERVICE):
                has_service = True
            for char in service.characteristics:
                props = ",".join(char.properties)
                chars.append(f"{char.uuid}[{props}]")
                if _char_matches(char, MEROSS_CHAR_WRITE):
                    has_write = True
                if _char_matches(char, MEROSS_CHAR_NOTIFY):
                    has_notify = True

        _LOGGER.debug(
            "%s %s cache_hit=%s meross_svc=%s write=%s notify=%s "
            "service_count=%s chars=%s",
            self.address,
            _LOG_SVC,
            cached is not None,
            has_service,
            has_write,
            has_notify,
            service_count,
            chars,
        )
        if not has_service or not has_write or not has_notify:
            _LOGGER.debug(
                "%s %s missing meross_svc=%s write=%s notify=%s "
                "expected svc=%s write=%s notify=%s",
                self.address,
                _LOG_SVC_FAIL,
                has_service,
                has_write,
                has_notify,
                MEROSS_GATT_SERVICE,
                MEROSS_CHAR_WRITE,
                MEROSS_CHAR_NOTIFY,
            )
            return False
        return True

    async def _async_connect_and_settle(self) -> BleakClientWithServiceCache:
        """Connect once, then wait for BlueZ connection-param retune to settle."""
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                self.name,
                max_attempts=1,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s %s establish_connection failed: %s",
                self.address,
                _LOG_SVC_FAIL,
                err,
            )
            raise
        _LOGGER.debug(
            "%s %s 连接成功，等待 %.1fs 再使用 GATT / 订阅 Notify",
            self.address,
            _LOG_SETTLE,
            GATT_POST_CONNECT_SETTLE,
        )
        await asyncio.sleep(GATT_POST_CONNECT_SETTLE)
        return client

    async def _async_rediscover_services(
        self, client: BleakClientWithServiceCache
    ) -> None:
        """Re-read GATT from BlueZ on the current connection (not RemoveDevice).

        Bleak returns the in-memory table if ``services`` is already set, so a
        plain ``get_services()`` is a no-op after an incomplete connect.
        """
        _LOGGER.debug(
            "%s %s 同连接重新拉取 GATT 服务（不 RemoveDevice）",
            self.address,
            _LOG_REDISCOVER,
        )
        await asyncio.sleep(GATT_REDISCOVER_SETTLE)
        backend = getattr(client, "_backend", client)
        with contextlib.suppress(Exception):
            backend.services = None
        try:
            get_services = getattr(backend, "_get_services", None)
            if get_services is not None:
                await get_services(dangerous_use_bleak_cache=False)
            else:
                public_get = getattr(client, "get_services", None)
                if public_get is not None:
                    await public_get()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s %s get_services failed: %s",
                self.address,
                _LOG_REDISCOVER,
                err,
            )

    async def _async_meross_gatt_ready(
        self, client: BleakClientWithServiceCache
    ) -> bool:
        if self._log_gatt_discovery(client):
            return True
        await self._async_rediscover_services(client)
        return self._log_gatt_discovery(client)

    async def _async_clear_gatt_cache_and_reconnect(
        self,
        client: BleakClientWithServiceCache,
    ) -> BleakClientWithServiceCache:
        """Disconnect, best-effort BlueZ RemoveDevice, then reconnect once."""
        with contextlib.suppress(Exception):
            await client.disconnect()

        cleared = False
        try:
            cleared = await clear_cache(self.address)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s %s clear_cache raised (non-BlueZ or D-Bus): %s",
                self.address,
                _LOG_CLEAR,
                err,
            )

        # Advertisement heard before RemoveDevice still points at a dead path.
        self._last_adv_monotonic = None
        _LOGGER.debug(
            "%s %s 补发现仍不完整，已断开并 clear_cache=%s；"
            "等待新广告后再连一次（Linux/BlueZ 有效）",
            self.address,
            _LOG_CLEAR,
            cleared,
        )
        await asyncio.sleep(GATT_INPROGRESS_COOLDOWN)
        await self._async_wait_for_connect_window(reason="GATT cache recovery")
        self._async_refresh_ble_device()
        self._log_ha_ble_cache("after clear_cache, before GATT reconnect")
        return await self._async_connect_and_settle()

    async def _async_establish_connection(self) -> BleakClientWithServiceCache:
        self._log_ha_ble_cache("before GATT connect")
        client = await self._async_connect_and_settle()
        if await self._async_meross_gatt_ready(client):
            return client
        if not self._bluez_remove_done:
            client = await self._async_clear_gatt_cache_and_reconnect(client)
            self._bluez_remove_done = True
            if await self._async_meross_gatt_ready(client):
                return client
        raise MerossBLEError(
            f"{self.address} incomplete GATT after rediscover/"
            "clear_cache; skip start_notify"
        )

    async def _async_start_notify(
        self,
        client: BleakClientWithServiceCache,
        callback: Callable[[int, bytearray], None],
    ) -> None:
        _LOGGER.debug(
            "%s %s subscribing char=%s",
            self.address,
            _LOG_NOTIFY,
            MEROSS_CHAR_NOTIFY,
        )
        try:
            await client.start_notify(MEROSS_CHAR_NOTIFY, callback)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s %s FAIL char=%s: %s",
                self.address,
                _LOG_NOTIFY,
                MEROSS_CHAR_NOTIFY,
                err,
            )
            raise
        _LOGGER.debug(
            "%s %s OK char=%s",
            self.address,
            _LOG_NOTIFY,
            MEROSS_CHAR_NOTIFY,
        )

    def _adv_is_fresh(self) -> bool:
        info = self._last_service_info()
        if info is not None:
            return (time.monotonic() - info.time) <= GATT_FRESH_ADV_SECONDS
        if self._last_adv_monotonic is None:
            return False
        return (time.monotonic() - self._last_adv_monotonic) <= GATT_FRESH_ADV_SECONDS

    async def _async_wait_for_connect_window(self, *, reason: str) -> None:
        """Wait for a live advertisement; macOS cache is not a connect window."""
        if self._wait_advertisement is None:
            return
        if self._adv_is_fresh():
            _LOGGER.debug(
                "%s: %s — heard advertisement ≤%.0fs ago, connecting",
                self.address,
                reason,
                GATT_FRESH_ADV_SECONDS,
            )
            return
        _LOGGER.debug(
            "%s: %s — waiting up to %.0fs for next advertisement",
            self.address,
            reason,
            GATT_ADV_WAIT_TIMEOUT,
        )
        if not await self._wait_advertisement(GATT_ADV_WAIT_TIMEOUT):
            _LOGGER.debug(
                "%s: %s — no advertisement within %.0fs; trying cached BLEDevice",
                self.address,
                reason,
                GATT_ADV_WAIT_TIMEOUT,
            )

    async def _async_prepare_gatt_attempt(self, attempt: int) -> None:
        """Connect immediately first; after a failure wait for a live advertisement."""
        if attempt > 1:
            await self._async_wait_for_connect_window(
                reason=f"GATT retry {attempt}/{self.retry_count}"
            )
        self._async_refresh_ble_device()

    async def _async_establish_connection(self) -> BleakClientWithServiceCache:
        """Open one GATT connection. Do not cancel in-flight CoreBluetooth connects."""
        self._async_refresh_ble_device()
        try:
            return await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                self.name,
                max_attempts=1,
            )
        except Exception:
            if self._gatt_gate is not None:
                self._gatt_gate.note_disconnected()
            raise

    def advertisement_changed(self, adv: MerossAdvertisement) -> bool:
        if self._last_adv is None:
            return True
        ignore = {"rssi", "address", "product_data"}
        old = {k: v for k, v in self._last_adv.data.items() if k not in ignore}
        new = {k: v for k, v in adv.data.items() if k not in ignore}
        # Events are handled via update_from_advertisement return value; do not
        # treat sticky/rebroadcast presses as a data change by themselves.
        return old != new

    def update_from_advertisement(self, adv: MerossAdvertisement) -> list[tuple[int, int]]:
        """Apply ad state; return new (non-duplicate) instantaneous events."""
        self._device = adv.device
        self._last_adv = adv
        self._last_adv_monotonic = time.monotonic()
        if "status" in adv.data or "alarm_status" in adv.data:
            self._data.update(adv.data)
        self._data["rssi"] = adv.rssi

        if not adv.events:
            return []

        if not self._event_req_id_bootstrapped:
            self._event_req_id_bootstrapped = True
            req_id, event_code = adv.events[0]
            self._last_accepted_req_id = req_id
            self._last_seen_req_id = req_id
            _LOGGER.debug(
                "%s: bootstrap event req_id=%s event=%#x — ignore sticky "
                "press from before HA start; wait for newer req_id",
                self.address,
                req_id,
                event_code,
            )
            return []

        new_events: list[tuple[int, int]] = []
        for req_id, event_code in adv.events:
            button_id = event_code & 0x03
            screen_id = (event_code >> 2) & 0x03
            logical = (
                ms700_logical_button(event_code)
                if self.model is MerossModel.MS700
                else None
            )

            prev_seen = self._last_seen_req_id
            if prev_seen is not None:
                seen_gap = (req_id - prev_seen) & 0xFF
                if 1 < seen_gap <= 128:
                    _LOGGER.warning(
                        "%s: BLE MISSED presses — req_id jumped %s → %s "
                        "(gap=%s). Advertisement not delivered to HA.",
                        self.address,
                        prev_seen,
                        req_id,
                        seen_gap - 1,
                    )
            self._last_seen_req_id = req_id

            if self.model is MerossModel.MS700:
                _LOGGER.debug(
                    "%s: MS700 adv event raw req_id=%s event=%#x "
                    "screen=%s button=%s logical=%s product_data=%s",
                    self.address,
                    req_id,
                    event_code,
                    screen_id,
                    button_id,
                    logical,
                    adv.data.get("product_data"),
                )
                if logical is None:
                    _LOGGER.warning(
                        "%s: MS700 UNMAPPED event=%#x (screen=%s button=%s)",
                        self.address,
                        event_code,
                        screen_id,
                        button_id,
                    )

            # Accept each report_reqId once. gap==0 → rebroadcast; gap>128 →
            # older stale packet after a newer id was already accepted.
            if self._last_accepted_req_id is not None:
                accept_gap = (req_id - self._last_accepted_req_id) & 0xFF
                if accept_gap == 0 or accept_gap > 128:
                    if accept_gap == 0 and self._stale_event_logged_for != req_id:
                        self._stale_event_logged_for = req_id
                        _LOGGER.debug(
                            "%s: sticky/rebroadcast req_id=%s event=%#x "
                            "— ignoring until req_id advances",
                            self.address,
                            req_id,
                            event_code,
                        )
                    continue

            self._last_accepted_req_id = req_id
            self._stale_event_logged_for = None
            new_events.append((req_id, event_code))
            _LOGGER.debug(
                "%s: ACCEPT event req_id=%s event=%#x logical=%s",
                self.address,
                req_id,
                event_code,
                logical,
            )
        return new_events

    def poll_needed(self, seconds_since_last_poll: float | None) -> bool:
        return False

    async def update(self) -> None:
        return None

    def _next_msg_id(self) -> int:
        self._msg_id = (self._msg_id + 1) & 0xFF
        if self._msg_id == 0:
            self._msg_id = 1
        return self._msg_id

    async def async_send_heartbeat(self) -> bool:
        """Send GATT heartbeat to wake firmware and refresh advertisements."""
        if self._gatt_gate is not None:
            async with self._gatt_gate.identify_claim():
                return await self._async_send_heartbeat_claimed()
        return await self._async_send_heartbeat_claimed()

    async def _async_send_heartbeat_claimed(self) -> bool:
        _LOGGER.debug("%s: heartbeat GATT write starting", self.address)
        raw = await self._request_special(
            lambda: build_heartbeat_frame(self.subdev_type, self._next_msg_id())
        )
        if not raw:
            _LOGGER.debug("%s: heartbeat write done (no notify ACK)", self.address)
            return True
        ok = parse_ack_success(raw)
        _LOGGER.debug(
            "%s: heartbeat write done ack_ok=%s notify=%s",
            self.address,
            ok,
            raw.hex(),
        )
        return ok

    async def identify(self) -> None:
        """Send Identify over GATT; firmware must ACK on the notify characteristic."""
        if self._gatt_gate is not None:
            async with self._gatt_gate.identify_claim():
                await self.async_identify_claimed()
            return
        await self.async_identify_claimed()

    async def async_identify_claimed(self) -> None:
        """Identify while an Identify claim is already held (bind path)."""
        _LOGGER.debug("%s: Identify GATT write starting", self.address)
        raw = await self._request_raw(
            lambda: build_identify_frame(self.subdev_type, self._next_msg_id())
        )
        if not raw:
            raise MerossBLEError(
                "Identify notify timeout (no ACK); firmware must respond on "
                f"{MEROSS_CHAR_NOTIFY}"
            )
        if not parse_ack_success(raw):
            raise MerossBLEError(
                f"Identify failed: non-success notify ACK: {raw.hex()}"
            )
        _LOGGER.debug(
            "%s: Identify done ack_ok notify=%s",
            self.address,
            raw.hex(),
        )

    async def fetch_temperature_history(
        self, start_idx: int = 0
    ) -> list[HistorySample]:
        """Fetch temperature history from start_idx to end of device buffer."""
        return await self._fetch_history(
            count_builder=build_temp_history_count_frame,
            data_builder=build_temp_history_data_frame,
            count_tag=TAG_TEMP_HISTORY_COUNT,
            data_tag=TAG_TEMP_HISTORY_DATA,
            scale=100.0,
            start_idx=start_idx,
        )

    async def fetch_humidity_history(self, start_idx: int = 0) -> list[HistorySample]:
        """Fetch humidity history from start_idx to end of device buffer."""
        return await self._fetch_history(
            count_builder=build_humi_history_count_frame,
            data_builder=build_humi_history_data_frame,
            count_tag=TAG_HUMI_HISTORY_COUNT,
            data_tag=TAG_HUMI_HISTORY_DATA,
            scale=10.0,
            start_idx=start_idx,
        )

    async def _fetch_history(
        self,
        *,
        count_builder: Callable[[int, int], bytes],
        data_builder: Callable[[int, int, int, int], bytes],
        count_tag: int,
        data_tag: int,
        scale: float,
        start_idx: int,
    ) -> list[HistorySample]:
        self._bluez_remove_done = False
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                await self._async_prepare_gatt_attempt(attempt)
                if self._gatt_gate is not None:
                    async with self._gatt_gate.history_session():
                        return await self._fetch_history_once(
                            count_builder=count_builder,
                            data_builder=data_builder,
                            count_tag=count_tag,
                            data_tag=data_tag,
                            scale=scale,
                            start_idx=start_idx,
                        )
                return await self._fetch_history_once(
                    count_builder=count_builder,
                    data_builder=data_builder,
                    count_tag=count_tag,
                    data_tag=data_tag,
                    scale=scale,
                    start_idx=start_idx,
                )
            except MerossBLEHistoryYield:
                raise
            except Exception as err:  # noqa: BLE001
                last_error = err
                log = _LOGGER.warning if attempt == self.retry_count else _LOGGER.debug
                log(
                    "%s history fetch failed (%s/%s): %s",
                    self.address,
                    attempt,
                    self.retry_count,
                    err,
                )
                if attempt < self.retry_count:
                    await asyncio.sleep(GATT_INPROGRESS_COOLDOWN)
        if last_error:
            raise MerossBLEError(str(last_error)) from last_error
        return []

    async def _fetch_history_once(
        self,
        *,
        count_builder: Callable[[int, int], bytes],
        data_builder: Callable[[int, int, int, int], bytes],
        count_tag: int,
        data_tag: int,
        scale: float,
        start_idx: int,
    ) -> list[HistorySample]:
        client = await self._async_establish_connection()
        notify = asyncio.Event()
        payload_box: dict[str, bytes] = {}

        def _on_notify(_handle: int, data: bytearray) -> None:
            payload_box["data"] = bytes(data)
            notify.set()

        samples: list[HistorySample] = []
        try:
            await self._async_start_notify(client, _on_notify)
            # Give CCCD enable time before first write (CoreBluetooth often needs this).
            await asyncio.sleep(GATT_NOTIFY_SUBSCRIBE_SETTLE)
            self._raise_if_history_should_yield()

            count_frame = count_builder(self.subdev_type, self._next_msg_id())
            _LOGGER.debug(
                "%s: history COUNT request tag=%#x write=%s",
                self.address,
                count_tag,
                count_frame.hex(),
            )
            count_raw = await self._exchange(
                client,
                notify,
                payload_box,
                lambda frame=count_frame: frame,
                write_with_response=False,
                timeout=GATT_NOTIFY_TIMEOUT,
            )
            _LOGGER.debug(
                "%s: history COUNT notify tag=%#x raw=%s",
                self.address,
                count_tag,
                count_raw.hex() if count_raw else "<empty>",
            )
            total = parse_history_count(count_raw, count_tag)
            if total is None:
                if not count_raw:
                    raise MerossBLEError(
                        f"No history count notify for tag {count_tag:#x} "
                        f"(timeout/empty); write={count_frame.hex()}"
                    )
                tags = [(tag, value.hex()) for tag, value in iter_tlvs(count_raw)]
                raise MerossBLEError(
                    f"Invalid history count response for tag {count_tag:#x}; "
                    f"tlvs={tags} raw={count_raw.hex()}"
                )
            _LOGGER.debug(
                "%s: history COUNT ok tag=%#x total=%s start_idx=%s",
                self.address,
                count_tag,
                total,
                start_idx,
            )
            if total <= start_idx:
                _LOGGER.debug(
                    "%s: history nothing new tag=%#x (total=%s <= start_idx=%s)",
                    self.address,
                    count_tag,
                    total,
                    start_idx,
                )
                return []

            cursor = start_idx
            end_exclusive = total
            while cursor < end_exclusive:
                self._raise_if_history_should_yield()
                page_end = min(cursor + HISTORY_PAGE_SIZE, end_exclusive) - 1
                start = cursor
                end = page_end
                data_frame = data_builder(
                    self.subdev_type, start, end, self._next_msg_id()
                )
                _LOGGER.debug(
                    "%s: history DATA request tag=%#x idx=%s-%s write=%s",
                    self.address,
                    data_tag,
                    start,
                    end,
                    data_frame.hex(),
                )
                page_raw = await self._exchange(
                    client,
                    notify,
                    payload_box,
                    lambda frame=data_frame: frame,
                    write_with_response=False,
                    timeout=GATT_NOTIFY_TIMEOUT,
                )
                _LOGGER.debug(
                    "%s: history DATA notify tag=%#x idx=%s-%s raw_len=%s",
                    self.address,
                    data_tag,
                    start,
                    end,
                    len(page_raw) if page_raw else 0,
                )
                page = parse_history_samples(page_raw, data_tag, scale=scale)
                if not page:
                    _LOGGER.warning(
                        "%s: empty history page %s-%s tag=%#x",
                        self.address,
                        cursor,
                        page_end,
                        data_tag,
                    )
                    break
                _LOGGER.debug(
                    "%s: history DATA parsed %s samples tag=%#x first=%s last=%s",
                    self.address,
                    len(page),
                    data_tag,
                    (page[0].timestamp.isoformat(), page[0].value),
                    (page[-1].timestamp.isoformat(), page[-1].value),
                )
                samples.extend(page)
                cursor = page_end + 1
            return samples
        finally:
            with contextlib.suppress(Exception):
                await client.stop_notify(MEROSS_CHAR_NOTIFY)
            await client.disconnect()

    async def _request_raw(self, frame: bytes) -> bytes:
        self._bluez_remove_done = False
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                await self._async_prepare_gatt_attempt(attempt)
                if self._gatt_gate is not None:
                    async with self._gatt_gate.identify_session():
                        raw = await self._execute_once(frame_factory)
                else:
                    raw = await self._execute_once(frame_factory)
                if raw or not require_notify:
                    return raw
                raise MerossBLENotifyTimeout(
                    "GATT notify timeout (no ACK); retrying on a new connection"
                )
            except Exception as err:  # noqa: BLE001
                last_error = err
                log = _LOGGER.warning if attempt == self.retry_count else _LOGGER.debug
                log(
                    "%s frame failed (%s/%s): %s",
                    self.address,
                    attempt,
                    self.retry_count,
                    str(err) or type(err).__name__,
                )
                if attempt < self.retry_count:
                    await asyncio.sleep(GATT_INPROGRESS_COOLDOWN)
        if last_error:
            raise MerossBLEError(str(last_error)) from last_error
        return b""

    async def _execute_once(self, frame: bytes) -> bytes:
        client = await self._async_establish_connection()
        notify = asyncio.Event()
        payload_box: dict[str, bytes] = {}

        def _on_notify(_handle: int, data: bytearray) -> None:
            payload_box["data"] = bytes(data)
            notify.set()

        try:
            await self._async_start_notify(client, _on_notify)
            return await self._exchange(
                client, notify, payload_box, frame_factory
            )
        finally:
            with contextlib.suppress(Exception):
                await client.stop_notify(MEROSS_CHAR_NOTIFY)
            await client.disconnect()
            if self._gatt_gate is not None:
                self._gatt_gate.note_disconnected()

    async def _exchange(
        self,
        client: BleakClientWithServiceCache,
        notify: asyncio.Event,
        payload_box: dict[str, bytes],
        frame_factory: Callable[[], bytes],
        *,
        write_with_response: bool = True,
        timeout: float = GATT_NOTIFY_TIMEOUT,
    ) -> bytes:
        notify.clear()
        payload_box.pop("data", None)
        frame = frame_factory()
        _LOGGER.debug(
            "%s %s 等待Notify应答 timeout=%.1fs frame=%s",
            self.address,
            _LOG_TIMEOUT,
            timeout,
            frame.hex(),
        )
        await client.write_gatt_char(
            MEROSS_CHAR_WRITE, frame, response=write_with_response
        )
        try:
            async with asyncio.timeout(timeout):
                await notify.wait()
        except TimeoutError:
            _LOGGER.debug(
                "%s %s Notify应答超时 timeout=%.1fs frame=%s",
                self.address,
                _LOG_TIMEOUT,
                timeout,
                frame.hex(),
            )
            return b""
        _LOGGER.debug(
            "%s %s Notify应答成功 timeout=%.1fs",
            self.address,
            _LOG_TIMEOUT,
            timeout,
        )
        return payload_box.get("data", b"")


DEVICE_CLASS_BY_MODEL: dict[MerossModel, Callable[..., MerossBLEDevice]] = {
    MerossModel.MS120: MerossBLEDevice,
    MerossModel.MS220: MerossBLEDevice,
    MerossModel.MS420: MerossBLEDevice,
    MerossModel.MS700: MerossBLEDevice,
}


def create_device(
    device: BLEDevice,
    model: MerossModel,
    retry_count: int = DEFAULT_RETRY_COUNT,
) -> MerossBLEDevice:
    """Factory for model-specific wrappers."""
    cls = DEVICE_CLASS_BY_MODEL.get(model, MerossBLEDevice)
    return cls(device, model, retry_count)
