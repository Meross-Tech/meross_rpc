"""Coordinator for Meross Bluetooth."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback

from .const import CONNECTABLE_MODELS, DEVICE_STARTUP_TIMEOUT, MerossModel
from .device import MerossBLEDevice
from .history import async_sync_ms120_history
from .parser import parse_advertisement_data

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

type MerossBLEConfigEntry = ConfigEntry[MerossBLEDataUpdateCoordinator]


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
        super()._async_handle_unavailable(service_info)
        self._was_unavailable = True
        _LOGGER.info("Device %s is unavailable", self.device_name)

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        self.ble_device = service_info.device
        self.device.update_ble_device(service_info.device)
        adv = parse_advertisement_data(
            service_info.device, service_info.advertisement, self.model
        )
        if not adv:
            return
        self._ready_event.set()
        changed = self.device.advertisement_changed(adv)
        new_events = self.device.update_from_advertisement(adv)
        self.last_new_events = new_events
        recovered = self._was_unavailable
        if not changed and not new_events and not recovered:
            return
        self._was_unavailable = False
        if new_events:
            _LOGGER.info(
                "%s: Meross BLE events=%s data_keys=%s",
                self.ble_device.address,
                new_events,
                sorted(k for k in adv.data if k in ("temperature", "humidity", "battery")),
            )
        else:
            _LOGGER.debug(
                "%s: Meross BLE data: %s events=%s",
                self.ble_device.address,
                adv.data,
                new_events,
            )
        super()._async_handle_bluetooth_event(service_info, change)
        if recovered and self.model is MerossModel.MS120:
            _LOGGER.info(
                "%s: device recovered from unavailable → scheduling history sync "
                "(force full firmware pull for local gap fill)",
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
            _LOGGER.info(
                "%s: history sync already running, skip schedule",
                self.ble_device.address,
            )
            return

        _LOGGER.info(
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
