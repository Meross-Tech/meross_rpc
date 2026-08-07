"""Event platform for Meross Bluetooth (MS220 doorbell / button)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MS220_EVENT_TYPES, MerossModel
from .coordinator import MerossBLEConfigEntry, MerossBLEDataUpdateCoordinator
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
    async_add_entities([MerossBLEMS220EventEntity(coordinator)])


class MerossBLEMS220EventEntity(MerossBLEEntity, EventEntity):
    """Fires on doorbell / big-button actions (ms220_ha.md report_event)."""

    _attr_translation_key = "ms220_actions"
    _attr_event_types = list(MS220_EVENT_TYPES.values())

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-actions"

    @callback
    def _handle_coordinator_update(self) -> None:
        for _req_id, event_code in self.coordinator.last_new_events:
            event_type = MS220_EVENT_TYPES.get(event_code)
            if event_type:
                self._trigger_event(event_type)
        super()._handle_coordinator_update()
