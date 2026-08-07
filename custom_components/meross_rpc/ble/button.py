"""Button platform: Identify (beep/flash)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MerossBLEConfigEntry, MerossBLEDataUpdateCoordinator
from .device import MerossBLEError
from .entity import MerossBLEEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerossBLEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([MerossBLEIdentifyButton(entry.runtime_data)])


class MerossBLEIdentifyButton(MerossBLEEntity, ButtonEntity):
    """Trigger device Identify (ble_ha.md §3.2)."""

    _attr_translation_key = "identify"

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-identify"

    async def async_press(self) -> None:
        try:
            await self.coordinator.device.identify()
        except MerossBLEError as err:
            raise HomeAssistantError(f"Identify failed: {err}") from err
