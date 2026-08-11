"""Light platform for Meross Bluetooth (MS220 night light)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MerossModel
from .coordinator import MerossBLEConfigEntry, MerossBLEDataUpdateCoordinator
from .device import MerossBLEError, MerossBLEMS220
from .entity import MerossBLEEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerossBLEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.model != MerossModel.MS220:
        return
    if not isinstance(coordinator.device, MerossBLEMS220):
        return
    async_add_entities([MerossBLENightLightEntity(coordinator)])


class MerossBLENightLightEntity(MerossBLEEntity, LightEntity):
    """MS220 night light — on/off only (no brightness)."""

    _attr_translation_key = "night_light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-night_light"
        self._device: MerossBLEMS220 = coordinator.device

    @property
    def is_on(self) -> bool | None:
        return self._device.night_light_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._device.async_set_night_light(True)
        except MerossBLEError as err:
            raise HomeAssistantError(f"Failed to turn on night light: {err}") from err
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._device.async_set_night_light(False)
        except MerossBLEError as err:
            raise HomeAssistantError(f"Failed to turn off night light: {err}") from err
        self.async_write_ha_state()
