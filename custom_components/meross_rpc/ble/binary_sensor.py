"""Binary sensors for Meross Bluetooth."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import BATTERY_LOW_THRESHOLD, MerossModel
from .coordinator import MerossBLEConfigEntry, MerossBLEDataUpdateCoordinator
from .entity import MerossBLEEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MerossBLEBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Binary sensor mapped to a parsed_data key."""

    value_key: str


COMMON_BINARY_SENSORS: tuple[MerossBLEBinarySensorEntityDescription, ...] = (
    MerossBLEBinarySensorEntityDescription(
        key="battery_low",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="battery",
    ),
)

MS220_BINARY_SENSORS: tuple[MerossBLEBinarySensorEntityDescription, ...] = (
    MerossBLEBinarySensorEntityDescription(
        key="door",
        translation_key="door",
        device_class=BinarySensorDeviceClass.DOOR,
        value_key="door_open",
    ),
    MerossBLEBinarySensorEntityDescription(
        key="vibration",
        translation_key="vibration",
        device_class=BinarySensorDeviceClass.VIBRATION,
        value_key="vibration",
    ),
    MerossBLEBinarySensorEntityDescription(
        key="alarm_door_open_long",
        translation_key="alarm_door_open_long",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="alarm_door_open_long",
    ),
    MerossBLEBinarySensorEntityDescription(
        key="alarm_door_closed_long",
        translation_key="alarm_door_closed_long",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="alarm_door_closed_long",
    ),
)

BINARY_SENSORS_BY_MODEL: dict[
    MerossModel, tuple[MerossBLEBinarySensorEntityDescription, ...]
] = {
    MerossModel.MS120: COMMON_BINARY_SENSORS,
    MerossModel.MS220: (*COMMON_BINARY_SENSORS, *MS220_BINARY_SENSORS),
    MerossModel.MS605: COMMON_BINARY_SENSORS,
    MerossModel.MS420: COMMON_BINARY_SENSORS,
    MerossModel.MS700: COMMON_BINARY_SENSORS,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerossBLEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    descriptions = BINARY_SENSORS_BY_MODEL.get(coordinator.model, ())
    entities: list[BinarySensorEntity] = [
        MerossBLEBinarySensor(coordinator, description)
        for description in descriptions
    ]
    entities.append(MerossBLEConnectivitySensor(coordinator))
    async_add_entities(entities)


class MerossBLEBinarySensor(MerossBLEEntity, BinarySensorEntity):
    entity_description: MerossBLEBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: MerossBLEDataUpdateCoordinator,
        description: MerossBLEBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.base_unique_id}-{description.key}"

    @property
    def is_on(self) -> bool | None:
        if self.entity_description.key == "battery_low":
            battery = self.parsed_data.get("battery")
            if not isinstance(battery, (int, float)):
                return None
            return battery <= BATTERY_LOW_THRESHOLD

        value = self.parsed_data.get(self.entity_description.value_key)
        if value is None:
            return None
        return bool(value)


class MerossBLEConnectivitySensor(MerossBLEEntity, BinarySensorEntity):
    """True while BLE advertisements are being received."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connectivity"

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-connectivity"

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.available
