"""Event platform for Meross Bluetooth (MS220 / MS700)."""

from __future__ import annotations

import logging

from homeassistant.components.event import (
    ATTR_MULTI_PRESS_COUNT,
    ButtonEventType,
    DoorbellEventType,
    EventDeviceClass,
    EventEntity,
    EventExtraStoredData,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    MS220_EVENT_BUTTON_DOUBLE,
    MS220_EVENT_BUTTON_SINGLE,
    MS220_EVENT_DOORBELL,
    MS700_BUTTON_COUNT,
    MerossModel,
    ms700_button_enabled,
    ms700_logical_button,
)
from .coordinator import MerossBLEConfigEntry, MerossBLEDataUpdateCoordinator
from .entity import MerossBLEEntity

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerossBLEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.model == MerossModel.MS220:
        async_add_entities(
            [
                MerossBLEMS220ButtonEventEntity(coordinator),
                MerossBLEMS220DoorbellEventEntity(coordinator),
            ]
        )
        return
    if coordinator.model == MerossModel.MS700:
        async_add_entities(
            [
                MerossBLEMS700ButtonEventEntity(coordinator, button_number)
                for button_number in range(1, MS700_BUTTON_COUNT + 1)
            ]
        )

        @callback
        def _sync_ms700_button_entities() -> None:
            """Enable/disable button entities from product_data screen_enable."""
            screen_enable = coordinator.device.data.get("screen_enable")
            if screen_enable is None:
                return
            registry = er.async_get(hass)
            domain = entry.domain
            for button_number in range(1, MS700_BUTTON_COUNT + 1):
                unique_id = f"{coordinator.base_unique_id}-button-{button_number}"
                entity_id = registry.async_get_entity_id(
                    Platform.EVENT, domain, unique_id
                )
                if entity_id is None:
                    continue
                reg_entry = registry.async_get(entity_id)
                if reg_entry is None:
                    continue
                want_enabled = ms700_button_enabled(button_number, screen_enable)
                if want_enabled:
                    if reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                        registry.async_update_entity(entity_id, disabled_by=None)
                elif reg_entry.disabled_by is None:
                    registry.async_update_entity(
                        entity_id,
                        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                    )

        entry.async_on_unload(coordinator.async_add_listener(_sync_ms700_button_entities))
        _sync_ms700_button_entities()


class _MerossBLEEventEntity(MerossBLEEntity, EventEntity):
    """Event entity that does not restore last press across restarts.

    Restoring would rewrite the previous Press into Logbook/Activity at HA
    startup time (looks like a real press). Only live _trigger_event counts.
    """

    async def async_get_last_event_data(self) -> EventExtraStoredData | None:
        return None


class MerossBLEMS220ButtonEventEntity(_MerossBLEEventEntity):
    """Physical button: single → press_end, double → multi_press_end (ms220.md)."""

    _attr_translation_key = "button"
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [
        ButtonEventType.PRESS_END,
        ButtonEventType.MULTI_PRESS_END,
    ]

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-button"

    @callback
    def _handle_coordinator_update(self) -> None:
        for _req_id, event_code in self.coordinator.last_new_events:
            if event_code == MS220_EVENT_BUTTON_SINGLE:
                self._trigger_event(ButtonEventType.PRESS_END)
            elif event_code == MS220_EVENT_BUTTON_DOUBLE:
                self._trigger_event(
                    ButtonEventType.MULTI_PRESS_END,
                    {ATTR_MULTI_PRESS_COUNT: 2},
                )
        super()._handle_coordinator_update()


class MerossBLEMS220DoorbellEventEntity(_MerossBLEEventEntity):
    """Doorbell mode ring events (report_event 0x06)."""

    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [DoorbellEventType.RING]

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-doorbell"

    @callback
    def _handle_coordinator_update(self) -> None:
        for _req_id, event_code in self.coordinator.last_new_events:
            if event_code == MS220_EVENT_DOORBELL:
                self._trigger_event(DoorbellEventType.RING)
        super()._handle_coordinator_update()


class MerossBLEMS700ButtonEventEntity(_MerossBLEEventEntity):
    """One of nine MS700 screen buttons; single click → press_end (ms700.md)."""

    _attr_translation_key = "ms700_button"
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [ButtonEventType.PRESS_END]

    def __init__(
        self, coordinator: MerossBLEDataUpdateCoordinator, button_number: int
    ) -> None:
        super().__init__(coordinator)
        self._button_number = button_number
        self._attr_unique_id = f"{coordinator.base_unique_id}-button-{button_number}"
        self._attr_translation_placeholders = {"number": str(button_number)}

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        screen_enable = self.parsed_data.get("screen_enable")
        if screen_enable is None:
            return True
        return ms700_button_enabled(self._button_number, screen_enable)

    @callback
    def _handle_coordinator_update(self) -> None:
        screen_enable = self.parsed_data.get("screen_enable")
        for req_id, event_code in self.coordinator.last_new_events:
            logical = ms700_logical_button(event_code)
            if logical != self._button_number:
                continue
            if screen_enable is not None and not ms700_button_enabled(
                self._button_number, screen_enable
            ):
                _LOGGER.debug(
                    "%s: ignore press on disabled screen button %s (req_id=%s)",
                    self.coordinator.ble_device.address,
                    self._button_number,
                    req_id,
                )
                continue
            _LOGGER.info(
                "%s: MS700 entity Button %s fired press_end (req_id=%s)",
                self.coordinator.ble_device.address,
                self._button_number,
                req_id,
            )
            self._trigger_event(ButtonEventType.PRESS_END)
            self.async_write_ha_state()
            return
        super()._handle_coordinator_update()
