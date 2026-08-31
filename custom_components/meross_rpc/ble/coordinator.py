"""Coordinator for Meross Bluetooth."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    ADVERTISEMENT_STALE_SECONDS,
    CONNECTABLE_MODELS,
    DEVICE_STARTUP_TIMEOUT,
    GATT_ADV_WAIT_TIMEOUT,
    MS220_EVENT_BUTTON_DOUBLE,
    MS220_EVENT_BUTTON_SINGLE,
    MS220_EVENT_DOORBELL,
    MerossModel,
    PERIODIC_ADVERTISEMENT_MODELS,
)
from .device import MerossBLEDevice, MerossBLEError
from .history import async_sync_ms120_history
from .parser import parse_advertisement_data

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

type MerossBLEConfigEntry = ConfigEntry[MerossBLEDataUpdateCoordinator]


_MS220_DOOR_ALARM_LOG_KEYS = (
    "door_open",
    "alarm_door_open_long",
    "alarm_door_closed_long",
    "alarm_vibration",
    "alarm_enable_map",
)


def _ms220_field_changed(
    prev: dict, current: dict, key: str, *, first_only_if_true: bool = False
) -> bool:
    if key not in current:
        return False
    if not prev:
        if first_only_if_true:
            return current.get(key) is True
        return True
    return prev.get(key) != current.get(key)


def _log_ms220_door_alarm_if_changed(
    address: str, prev: dict, current: dict
) -> None:
    """INFO for MS220 door open/close and long-open / long-closed / vibration alarms."""
    if not any(key in current for key in _MS220_DOOR_ALARM_LOG_KEYS):
        return

    if _ms220_field_changed(prev, current, "door_open"):
        door_open = current["door_open"]
        if door_open is not None:
            _LOGGER.info(
                "%s: MS220 door %s",
                address,
                "open" if door_open else "closed",
            )

    for key, label in (
        ("alarm_door_open_long", "open too long"),
        ("alarm_door_closed_long", "closed too long"),
        ("alarm_vibration", "vibration"),
    ):
        if not _ms220_field_changed(
            prev, current, key, first_only_if_true=True
        ):
            continue
        active = current.get(key) is True
        _LOGGER.info(
            "%s: MS220 alarm %s %s (status=0x%x enable_map=0x%x)",
            address,
            label,
            "on" if active else "cleared",
            current.get("alarm_status", 0),
            current.get("alarm_enable_map", 0),
        )

    if _ms220_field_changed(prev, current, "alarm_enable_map"):
        _LOGGER.debug(
            "%s: MS220 alarm enable_map=0x%x "
            "enable_open=%s enable_closed=%s enable_vibration=%s",
            address,
            current.get("alarm_enable_map", 0),
            current.get("alarm_enable_door_open_long"),
            current.get("alarm_enable_door_closed_long"),
            current.get("alarm_enable_vibration"),
        )


_MS220_EVENT_LABELS = {
    MS220_EVENT_DOORBELL: "doorbell",
    MS220_EVENT_BUTTON_SINGLE: "button single",
    MS220_EVENT_BUTTON_DOUBLE: "button double",
}


def _log_ms220_events(
    address: str, accepted: list[tuple[int, int]], raw: list[tuple[int, int]]
) -> None:
    """INFO for MS220 doorbell / button advertisements."""
    accepted_ids = {req_id for req_id, _event in accepted}
    for req_id, event_code in accepted:
        label = _MS220_EVENT_LABELS.get(event_code, f"event={event_code:#x}")
        _LOGGER.info(
            "%s: MS220 %s (req_id=%s)",
            address,
            label,
            req_id,
        )
    for req_id, event_code in raw:
        if req_id in accepted_ids:
            continue
        if event_code not in _MS220_EVENT_LABELS:
            continue
        _LOGGER.debug(
            "%s: MS220 %s ignored sticky/rebroadcast (req_id=%s)",
            address,
            _MS220_EVENT_LABELS[event_code],
            req_id,
        )


def _meross_service_data_hex(service_info: bluetooth.BluetoothServiceInfoBleak) -> str:
    """Return Meross service-data payload as hex (empty if missing)."""
    advertisement = service_info.advertisement
    if not advertisement.service_data:
        return ""
    for key, value in advertisement.service_data.items():
        key_l = str(key).lower().replace("-", "")
        if "be30" in key_l:
            return bytes(value).hex()
    # Fallback: first service_data blob
    first = next(iter(advertisement.service_data.values()), None)
    return bytes(first).hex() if first is not None else ""


class MerossBLEDataUpdateCoordinator(ActiveBluetoothDataUpdateCoordinator[None]):
    """Subscribe to one device's advertisements."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        ble_device: BLEDevice,
        device: MerossBLEDevice,
        base_unique_id: str,
        device_name: str,
        connectable: bool,
        model: MerossModel,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass=hass,
            logger=logger,
            address=ble_device.address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            connectable=connectable,
        )
        self.ble_device = ble_device
        self.device = device
        self.device_name = device_name
        self.base_unique_id = base_unique_id
        self.model = model
        self.config_entry = config_entry
        self._ready_event = asyncio.Event()
        self._was_unavailable = True
        self._history_lock = asyncio.Lock()
        self._history_task: asyncio.Task[None] | None = None
        # After BLE unavailable, re-pull full firmware buffer (ring buffer may wrap).
        self.history_force_full_resync = False
        # Instantaneous events from the latest advertisement (after dedup)
        self.last_new_events: list[tuple[int, int]] = []
        self._stale_unsub: CALLBACK_TYPE | None = None
        self._adv_waiters: list[asyncio.Event] = []
        self._gatt_status_lock = asyncio.Lock()
        self._gatt_status_task: asyncio.Task[None] | None = None

    @callback
    def _async_notify_advertisement_waiters(self) -> None:
        for event in self._adv_waiters:
            event.set()

    async def async_wait_next_advertisement(
        self, timeout: float = GATT_ADV_WAIT_TIMEOUT
    ) -> bool:
        """Wait until the next parseable advertisement for this device."""
        event = asyncio.Event()
        self._adv_waiters.append(event)
        try:
            async with asyncio.timeout(timeout):
                await event.wait()
            return True
        except TimeoutError:
            return False
        finally:
            self._adv_waiters.remove(event)

    @callback
    def _async_cancel_stale_timer(self) -> None:
        if self._stale_unsub is not None:
            self._stale_unsub()
            self._stale_unsub = None

    @callback
    def _async_schedule_stale_timer(self) -> None:
        """Restart watchdog after a parseable advertisement."""
        self._async_cancel_stale_timer()
        self._stale_unsub = async_call_later(
            self.hass,
            ADVERTISEMENT_STALE_SECONDS,
            self._async_advertisement_stale,
        )

    @callback
    def _async_advertisement_stale(self, _now: datetime) -> None:
        """Force unavailable when advertisements stop (macOS Bleak cache workaround)."""
        self._stale_unsub = None
        if not self.available:
            return
        self._available = False
        self._was_unavailable = True
        _LOGGER.info(
            "%s: %s no BLE advertisement for %ss → marking unavailable",
            self.address,
            self.model.value.upper(),
            ADVERTISEMENT_STALE_SECONDS,
        )
        self.async_update_listeners()

    @callback
    def _needs_poll(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        return (
            self.hass.state is CoreState.running
            and self.connectable
            and self.device.poll_needed(seconds_since_last_poll)
            and bool(
                bluetooth.async_ble_device_from_address(
                    self.hass, service_info.device.address, connectable=True
                )
            )
        )

    async def _async_update(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        await self.device.update()

    @callback
    def _async_handle_unavailable(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        # Meross battery devices often pause ads for 30s–60s when state is
        # unchanged. HA's async_track_unavailable (~30s) is too aggressive and
        # would cancel our 195s stale timer. Keep last name only; offline is
        # decided exclusively by _async_advertisement_stale.
        self._last_name = service_info.name
        _LOGGER.debug(
            "%s: HA bluetooth reported unavailable for %s "
            "(ignored; offline after %ss without ads)",
            self.address,
            self.device_name,
            ADVERTISEMENT_STALE_SECONDS,
        )

    @callback
    def _async_stop(self) -> None:
        self._async_cancel_stale_timer()
        super()._async_stop()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        self.ble_device = service_info.device
        self.device.update_ble_device(service_info.device)
        recovered = self._was_unavailable or not self.available
        raw_hex = _meross_service_data_hex(service_info) or "(none)"

        if recovered:
            _LOGGER.debug(
                "%s: %s BLE packet after unavailable rssi=%s "
                "change=%s name=%s service_data=%s",
                service_info.address,
                self.model.value.upper(),
                service_info.advertisement.rssi,
                change,
                service_info.name or service_info.advertisement.local_name,
                raw_hex,
            )
        else:
            _LOGGER.debug(
                "%s: %s BLE packet received rssi=%s change=%s name=%s "
                "service_data=%s",
                service_info.address,
                self.model.value.upper(),
                service_info.advertisement.rssi,
                change,
                service_info.name or service_info.advertisement.local_name,
                raw_hex,
            )

        adv = parse_advertisement_data(
            service_info.device, service_info.advertisement, self.model
        )
        if not adv or "status" not in adv.data:
            if recovered:
                _LOGGER.debug(
                    "%s: %s ignored incomplete advertisement after "
                    "unavailable (service_data=%s)",
                    service_info.address,
                    self.model.value.upper(),
                    raw_hex,
                )
                self.async_schedule_gatt_status_sync()
            else:
                _LOGGER.debug(
                    "%s: %s BLE packet could not be parsed",
                    service_info.address,
                    self.model.value.upper(),
                )
            return
        _LOGGER.debug(
            "%s: %s BLE packet parsed data=%s events=%s",
            adv.address,
            self.model.value.upper(),
            adv.data,
            adv.events,
        )
        prev_data = dict(self.device.data)
        changed = self.device.advertisement_changed(adv)
        new_events = self.device.update_from_advertisement(adv)
        # macOS rebroadcasts the last closed payload when the radio
        # wakes. Applying it would go available+closed and hide the
        # following door-open advertisement.
        if (
            recovered
            and self.model is MerossModel.MS220
            and not changed
            and not new_events
        ):
            _LOGGER.debug(
                "%s: MS220 ignored cached rebroadcast after unavailable "
                "(door_open=%s service_data=%s)",
                adv.address,
                adv.data.get("door_open"),
                raw_hex,
            )
            self.async_schedule_gatt_status_sync()
            return
        self._ready_event.set()
        if (
            self.model in PERIODIC_ADVERTISEMENT_MODELS
            or changed
            or new_events
            or recovered
        ):
            self._async_schedule_stale_timer()
        self._async_notify_advertisement_waiters()
        if self.model is MerossModel.MS220:
            _log_ms220_door_alarm_if_changed(adv.address, prev_data, adv.data)
            _log_ms220_events(adv.address, new_events, adv.events)
        self.last_new_events = new_events
        if recovered:
            _LOGGER.debug(
                "%s: %s recovered from unavailable "
                "(door_open=%s changed=%s events=%s)",
                adv.address,
                self.model.value.upper(),
                adv.data.get("door_open"),
                changed,
                new_events,
            )
            self._was_unavailable = False
        elif not changed and not new_events:
            _LOGGER.debug(
                "%s: %s BLE packet unchanged (no entity update)",
                adv.address,
                self.model.value.upper(),
            )
        elif new_events:
            _LOGGER.debug(
                "%s: Meross BLE events=%s",
                self.ble_device.address,
                new_events,
            )
        super()._async_handle_bluetooth_event(service_info, change)
        if recovered and self.model is MerossModel.MS120:
            _LOGGER.debug(
                "%s: recovered from unavailable → scheduling history sync",
                self.ble_device.address,
            )
            self.history_force_full_resync = True
            self.async_schedule_history_sync()

    @callback
    def async_schedule_gatt_status_sync(self) -> None:
        """MS220 on macOS: GATT heartbeat when ads lack service_data."""
        if self.model is not MerossModel.MS220 or sys.platform != "darwin":
            return
        if not self._was_unavailable and self.available:
            return
        if self._gatt_status_task and not self._gatt_status_task.done():
            _LOGGER.debug(
                "%s: MS220 GATT status sync already running, skip schedule",
                self.address,
            )
            return
        _LOGGER.debug(
            "%s: MS220 scheduling GATT status sync (heartbeat)",
            self.address,
        )

        async def _run() -> None:
            async with self._gatt_status_lock:
                if not self._was_unavailable and self.available:
                    return
                try:
                    await self.device.async_send_heartbeat()
                except MerossBLEError as err:
                    _LOGGER.warning(
                        "%s: MS220 GATT heartbeat failed: %s",
                        self.address,
                        err,
                    )
                    return
                if await self.async_wait_next_advertisement(GATT_ADV_WAIT_TIMEOUT):
                    _LOGGER.debug(
                        "%s: MS220 GATT status sync received status advertisement",
                        self.address,
                    )
                    return
                _LOGGER.debug(
                    "%s: MS220 GATT status sync timed out after %.0fs",
                    self.address,
                    GATT_ADV_WAIT_TIMEOUT,
                )

        self._gatt_status_task = self.hass.async_create_task(
            _run(), name=f"meross_rpc_ble_gatt_status_{self.base_unique_id}"
        )

    @callback
    def async_schedule_history_sync(self) -> None:
        """Schedule MS120 local history import (setup / reconnect)."""
        if self.model is not MerossModel.MS120:
            return
        if self._history_task and not self._history_task.done():
            _LOGGER.debug(
                "%s: history sync already running, skip schedule",
                self.ble_device.address,
            )
            return

        _LOGGER.debug(
            "%s: scheduling MS120 history sync task",
            self.ble_device.address,
        )

        async def _run() -> None:
            async with self._history_lock:
                await async_sync_ms120_history(self.hass, self)

        self._history_task = self.hass.async_create_task(
            _run(), name=f"meross_rpc_ble_history_{self.base_unique_id}"
        )

    async def async_wait_ready(self) -> bool:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(DEVICE_STARTUP_TIMEOUT):
                await self._ready_event.wait()
            return True
        return False

    def model_is_connectable(self) -> bool:
        return self.model in CONNECTABLE_MODELS
