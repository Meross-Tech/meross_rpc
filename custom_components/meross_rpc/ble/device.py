"""Device wrappers: advertisement state + optional Meross GATT control."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .const import (
    DEFAULT_RETRY_COUNT,
    HISTORY_PAGE_SIZE,
    MEROSS_CHAR_NOTIFY,
    MEROSS_CHAR_WRITE,
    MODEL_TO_SUBDEV,
    TAG_HUMI_HISTORY_COUNT,
    TAG_HUMI_HISTORY_DATA,
    TAG_TEMP_HISTORY_COUNT,
    TAG_TEMP_HISTORY_DATA,
    MerossModel,
    ms700_logical_button,
)
from .parser import MerossAdvertisement
from .protocol import (
    HistorySample,
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


class MerossBLEError(Exception):
    """Device-layer error."""


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
        self._data.update(adv.data)
        self._data["rssi"] = adv.rssi

        if not adv.events:
            return []

        if not self._event_req_id_bootstrapped:
            self._event_req_id_bootstrapped = True
            req_id, event_code = adv.events[0]
            self._last_accepted_req_id = req_id
            self._last_seen_req_id = req_id
            _LOGGER.info(
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
                _LOGGER.info(
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
                    if (
                        self.model is MerossModel.MS700
                        and accept_gap == 0
                        and self._stale_event_logged_for != req_id
                    ):
                        self._stale_event_logged_for = req_id
                        _LOGGER.info(
                            "%s: MS700 sticky/rebroadcast req_id=%s event=%#x "
                            "(logical=%s) — ignoring until req_id advances",
                            self.address,
                            req_id,
                            event_code,
                            logical,
                        )
                    continue

            self._last_accepted_req_id = req_id
            self._stale_event_logged_for = None
            new_events.append((req_id, event_code))
            if self.model is MerossModel.MS700:
                _LOGGER.info(
                    "%s: MS700 ACCEPT press → HA Button %s "
                    "(req_id=%s event=%#x)",
                    self.address,
                    logical,
                    req_id,
                    event_code,
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

    async def identify(self) -> bool:
        """Send Identify (beep/flash) over GATT."""
        frame = build_identify_frame(self.subdev_type, self._next_msg_id())
        _LOGGER.info(
            "%s: Identify GATT write starting frame=%s",
            self.address,
            frame.hex(),
        )
        raw = await self._request_raw(frame)
        # No notify still counts as success for identify (device may not ACK)
        if not raw:
            _LOGGER.info(
                "%s: Identify write done (no notify ACK)",
                self.address,
            )
            return True
        ok = parse_ack_success(raw)
        _LOGGER.info(
            "%s: Identify write done ack_ok=%s notify=%s",
            self.address,
            ok,
            raw.hex(),
        )
        return ok

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
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                return await self._fetch_history_once(
                    count_builder=count_builder,
                    data_builder=data_builder,
                    count_tag=count_tag,
                    data_tag=data_tag,
                    scale=scale,
                    start_idx=start_idx,
                )
            except Exception as err:  # noqa: BLE001
                last_error = err
                _LOGGER.warning(
                    "%s history fetch failed (%s/%s): %s",
                    self.address,
                    attempt,
                    self.retry_count,
                    err,
                )
                await asyncio.sleep(0.5)
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
        client = await establish_connection(
            BleakClientWithServiceCache,
            self._device,
            self.name,
            max_attempts=2,
        )
        notify = asyncio.Event()
        payload_box: dict[str, bytes] = {}

        def _on_notify(_handle: int, data: bytearray) -> None:
            payload_box["data"] = bytes(data)
            notify.set()

        samples: list[HistorySample] = []
        try:
            await client.start_notify(MEROSS_CHAR_NOTIFY, _on_notify)
            # Give CCCD enable time before first write (CoreBluetooth often needs this).
            await asyncio.sleep(0.5)

            count_frame = count_builder(self.subdev_type, self._next_msg_id())
            _LOGGER.info(
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
                timeout=10.0,
            )
            _LOGGER.info(
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
            _LOGGER.info(
                "%s: history COUNT ok tag=%#x total=%s start_idx=%s",
                self.address,
                count_tag,
                total,
                start_idx,
            )
            if total <= start_idx:
                _LOGGER.info(
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
                page_end = min(cursor + HISTORY_PAGE_SIZE, end_exclusive) - 1
                start = cursor
                end = page_end
                data_frame = data_builder(
                    self.subdev_type, start, end, self._next_msg_id()
                )
                _LOGGER.info(
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
                    timeout=10.0,
                )
                _LOGGER.info(
                    "%s: history DATA notify tag=%#x idx=%s-%s raw_len=%s raw=%s",
                    self.address,
                    data_tag,
                    start,
                    end,
                    len(page_raw) if page_raw else 0,
                    page_raw.hex() if page_raw else "<empty>",
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
                if data_tag == TAG_TEMP_HISTORY_DATA:
                    # Decode first record raw int16 for unit debugging
                    for tag, value in iter_tlvs(page_raw):
                        if tag == data_tag and len(value) >= 8:
                            raw16 = int.from_bytes(value[6:8], "big", signed=True)
                            _LOGGER.info(
                                "%s: TEMP RAW sample0 idx=%s raw_int16=%s "
                                "/100=%.2f°C (≈%.2f°F) parsed0=%s",
                                self.address,
                                int.from_bytes(value[0:2], "big"),
                                raw16,
                                raw16 / 100,
                                raw16 / 100 * 9 / 5 + 32,
                                page[0].value,
                            )
                            break
                _LOGGER.info(
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
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                return await self._execute_once(frame)
            except Exception as err:  # noqa: BLE001
                last_error = err
                _LOGGER.debug(
                    "%s frame failed (%s/%s): %s",
                    self.address,
                    attempt,
                    self.retry_count,
                    err,
                )
                await asyncio.sleep(0.5)
        if last_error:
            raise MerossBLEError(str(last_error)) from last_error
        return b""

    async def _execute_once(self, frame: bytes) -> bytes:
        client = await establish_connection(
            BleakClientWithServiceCache,
            self._device,
            self.name,
            max_attempts=2,
        )
        notify = asyncio.Event()
        payload_box: dict[str, bytes] = {}

        def _on_notify(_handle: int, data: bytearray) -> None:
            payload_box["data"] = bytes(data)
            notify.set()

        try:
            await client.start_notify(MEROSS_CHAR_NOTIFY, _on_notify)
            return await self._exchange(
                client, notify, payload_box, lambda: frame
            )
        finally:
            with contextlib.suppress(Exception):
                await client.stop_notify(MEROSS_CHAR_NOTIFY)
            await client.disconnect()

    async def _exchange(
        self,
        client: BleakClientWithServiceCache,
        notify: asyncio.Event,
        payload_box: dict[str, bytes],
        frame_factory: Callable[[], bytes],
        *,
        write_with_response: bool = True,
        timeout: float = 5.0,
    ) -> bytes:
        notify.clear()
        payload_box.pop("data", None)
        frame = frame_factory()
        await client.write_gatt_char(
            MEROSS_CHAR_WRITE, frame, response=write_with_response
        )
        try:
            async with asyncio.timeout(timeout):
                await notify.wait()
        except TimeoutError:
            _LOGGER.info(
                "%s: no notify within %.1fs for frame %s",
                self.address,
                timeout,
                frame.hex(),
            )
            return b""
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
