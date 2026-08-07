"""Switch platform for Meross Bluetooth."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MerossBLEConfigEntry, MerossBLEDataUpdateCoordinator
from .device import MerossBLEError, MerossBLESwitch
from .entity import MerossBLEEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerossBLEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if not isinstance(coordinator.device, MerossBLESwitch):
        return
    async_add_entities([MerossBLESwitchEntity(coordinator)])


class MerossBLESwitchEntity(MerossBLEEntity, SwitchEntity):
    _attr_name = None

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-switch"
        self._device: MerossBLESwitch = coordinator.device

    @property
    def is_on(self) -> bool:
        return self._device.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._device.turn_on()
        except MerossBLEError as err:
            raise HomeAssistantError(f"Failed to turn on: {err}") from err
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._device.turn_off()
        except MerossBLEError as err:
            raise HomeAssistantError(f"Failed to turn off: {err}") from err
        self.async_write_ha_state()
