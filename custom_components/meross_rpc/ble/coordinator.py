"""Coordinator for Meross Bluetooth."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    MerossModel,
)
from .device import MerossBLEDevice
from .history import async_sync_ms120_history
from .parser import parse_advertisement_data

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

type MerossBLEConfigEntry = ConfigEntry[MerossBLEDataUpdateCoordinator]


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
        _LOGGER.debug(
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

        _LOGGER.debug(
            "%s: %s BLE packet received rssi=%s change=%s name=%s service_data=%s",
            service_info.address,
            self.model.value.upper(),
            service_info.advertisement.rssi,
            change,
            service_info.name or service_info.advertisement.local_name,
            _meross_service_data_hex(service_info) or "(none)",
        )

        adv = parse_advertisement_data(
            service_info.device, service_info.advertisement, self.model
        )
        if not adv:
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
        self._ready_event.set()
        self._async_schedule_stale_timer()
        self._async_notify_advertisement_waiters()
        changed = self.device.advertisement_changed(adv)
        new_events = self.device.update_from_advertisement(adv)
        self.last_new_events = new_events
        recovered = self._was_unavailable or not self.available
        if recovered:
            _LOGGER.debug(
                "%s: %s recovered from unavailable (changed=%s events=%s)",
                adv.address,
                self.model.value.upper(),
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
        # Always notify entities so availability recovers after the stale timer
        # even when broadcast payload is unchanged.
        super()._async_handle_bluetooth_event(service_info, change)
        if recovered and self.model is MerossModel.MS120:
            _LOGGER.debug(
                "%s: recovered from unavailable → scheduling history sync",
                self.ble_device.address,
            )
            self.history_force_full_resync = True
            self.async_schedule_history_sync()

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
