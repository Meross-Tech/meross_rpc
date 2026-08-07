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
)
from .parser import MerossAdvertisement
from .protocol import (
    HistorySample,
    build_humi_history_count_frame,
    build_humi_history_data_frame,
    build_identify_frame,
    build_temp_history_count_frame,
    build_temp_history_data_frame,
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
        # Event dedup: report_event -> last report_reqId (ble_ha.md §2)
        self._last_event_ids: dict[int, int] = {}

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
        return old != new or bool(adv.events)

    def update_from_advertisement(self, adv: MerossAdvertisement) -> list[tuple[int, int]]:
        """Apply ad state; return new (non-duplicate) instantaneous events."""
        self._device = adv.device
        self._last_adv = adv
        self._data.update(adv.data)
        self._data["rssi"] = adv.rssi

        new_events: list[tuple[int, int]] = []
        for req_id, event_code in adv.events:
            last = self._last_event_ids.get(event_code)
            if last == req_id:
                continue
            self._last_event_ids[event_code] = req_id
            new_events.append((req_id, event_code))
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
        raw = await self._request_raw(frame)
        # No notify still counts as success for identify (device may not ACK)
        if not raw:
            return True
        return parse_ack_success(raw)

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
                _LOGGER.debug(
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

            count_raw = await self._exchange(
                client,
                notify,
                payload_box,
                lambda: count_builder(self.subdev_type, self._next_msg_id()),
            )
            total = parse_history_count(count_raw, count_tag)
            if total is None:
                raise MerossBLEError(f"Invalid history count response for tag {count_tag:#x}")
            if total <= start_idx:
                return []

            cursor = start_idx
            end_exclusive = total
            while cursor < end_exclusive:
                page_end = min(cursor + HISTORY_PAGE_SIZE, end_exclusive) - 1
                start = cursor
                end = page_end
                page_raw = await self._exchange(
                    client,
                    notify,
                    payload_box,
                    lambda s=start, e=end: data_builder(
                        self.subdev_type, s, e, self._next_msg_id()
                    ),
                )
                page = parse_history_samples(page_raw, data_tag, scale=scale)
                if not page:
                    _LOGGER.debug(
                        "%s: empty history page %s-%s tag=%#x",
                        self.address,
                        cursor,
                        page_end,
                        data_tag,
                    )
                    break
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
    ) -> bytes:
        notify.clear()
        payload_box.pop("data", None)
        frame = frame_factory()
        await client.write_gatt_char(MEROSS_CHAR_WRITE, frame, response=True)
        try:
            async with asyncio.timeout(5):
                await notify.wait()
        except TimeoutError:
            _LOGGER.debug("%s: no notify for frame %s", self.address, frame.hex())
            return b""
        return payload_box.get("data", b"")


class MerossBLESwitch(MerossBLEDevice):
    """Connectable switch-like device (MS220/MS605). Control TLV TBD per product doc."""

    def __init__(
        self,
        device: BLEDevice,
        model: MerossModel = MerossModel.MS220,
        retry_count: int = DEFAULT_RETRY_COUNT,
    ) -> None:
        super().__init__(device, model, retry_count)
        self._data.setdefault("isOn", False)

    @property
    def is_on(self) -> bool:
        return bool(self._data.get("isOn"))

    def poll_needed(self, seconds_since_last_poll: float | None) -> bool:
        if seconds_since_last_poll is None:
            return True
        return seconds_since_last_poll > 60 * 60

    async def turn_on(self) -> bool:
        """Placeholder until product TLV for on is published."""
        _LOGGER.warning(
            "%s: turn_on TLV not defined in product HA doc yet", self.address
        )
        raise MerossBLEError("Switch control TLV not defined for this model yet")

    async def turn_off(self) -> bool:
        """Placeholder until product TLV for off is published."""
        _LOGGER.warning(
            "%s: turn_off TLV not defined in product HA doc yet", self.address
        )
        raise MerossBLEError("Switch control TLV not defined for this model yet")


DEVICE_CLASS_BY_MODEL: dict[MerossModel, Callable[..., MerossBLEDevice]] = {
    MerossModel.MS120: MerossBLEDevice,
    MerossModel.MS220: MerossBLEDevice,  # door / vibration / button events (ms220_ha.md)
    MerossModel.MS605: MerossBLESwitch,
    MerossModel.MS420: MerossBLESwitch,
    MerossModel.MS700: MerossBLESwitch,
}


def create_device(
    device: BLEDevice,
    model: MerossModel,
    retry_count: int = DEFAULT_RETRY_COUNT,
) -> MerossBLEDevice:
    """Factory for model-specific wrappers."""
    cls = DEVICE_CLASS_BY_MODEL.get(model, MerossBLEDevice)
    if cls is MerossBLESwitch:
        return MerossBLESwitch(device, model=model, retry_count=retry_count)
    return cls(device, model, retry_count)
