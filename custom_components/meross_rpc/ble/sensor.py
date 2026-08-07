"""Sensor platform for Meross Bluetooth."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfDensity,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MerossModel
from .coordinator import MerossBLEConfigEntry, MerossBLEDataUpdateCoordinator
from .entity import MerossBLEEntity

PARALLEL_UPDATES = 0

SENSORS_BY_MODEL: dict[MerossModel, tuple[str, ...]] = {
    MerossModel.MS120: (
        "battery",
        "temperature",
        "humidity",
        "dew_point",
        "absolute_humidity",
        "vpd",
    ),
    MerossModel.MS220: ("battery",),
    MerossModel.MS605: ("battery",),
    MerossModel.MS420: ("battery",),
    MerossModel.MS700: ("battery",),
}


@dataclass(frozen=True, kw_only=True)
class MerossBLESensorEntityDescription(SensorEntityDescription):
    """Sensor description."""


SENSOR_TYPES: dict[str, MerossBLESensorEntityDescription] = {
    "rssi": MerossBLESensorEntityDescription(
        key="rssi",
        translation_key="bluetooth_signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "battery": MerossBLESensorEntityDescription(
        key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "temperature": MerossBLESensorEntityDescription(
        key="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "humidity": MerossBLESensorEntityDescription(
        key="humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "dew_point": MerossBLESensorEntityDescription(
        key="dew_point",
        translation_key="dew_point",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "absolute_humidity": MerossBLESensorEntityDescription(
        key="absolute_humidity",
        translation_key="absolute_humidity",
        native_unit_of_measurement=UnitOfDensity.GRAMS_PER_CUBIC_METER,
        device_class=SensorDeviceClass.ABSOLUTE_HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "vpd": MerossBLESensorEntityDescription(
        key="vpd",
        translation_key="vpd",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerossBLEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    keys = SENSORS_BY_MODEL.get(coordinator.model, ("battery",))
    entities: list[SensorEntity] = [
        MerossBLESensor(coordinator, key) for key in keys if key in SENSOR_TYPES
    ]
    entities.append(MerossBLERSSISensor(coordinator))
    async_add_entities(entities)


class MerossBLESensor(MerossBLEEntity, SensorEntity):
    entity_description: MerossBLESensorEntityDescription

    def __init__(
        self, coordinator: MerossBLEDataUpdateCoordinator, sensor: str
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = SENSOR_TYPES[sensor]
        self._sensor = sensor
        self._attr_unique_id = f"{coordinator.base_unique_id}-{sensor}"

    @property
    def native_value(self) -> float | int | None:
        value = self.parsed_data.get(self._sensor)
        if isinstance(value, (int, float)):
            return value
        return None


class MerossBLERSSISensor(MerossBLESensor):
    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "rssi")

    @property
    def native_value(self) -> int | None:
        if service_info := async_last_service_info(
            self.hass, self._address, self.coordinator.connectable
        ):
            return service_info.rssi
        return None
