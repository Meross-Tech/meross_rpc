"""Base entity for Meross Bluetooth."""

from __future__ import annotations

from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, LOGGER, MANUFACTURER, MODEL_FRIENDLY_NAME
from .coordinator import MerossBLEDataUpdateCoordinator


class MerossBLEEntity(
    PassiveBluetoothCoordinatorEntity[MerossBLEDataUpdateCoordinator]
):
    """Base Meross BLE entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MerossBLEDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._address = coordinator.ble_device.address
        # Prefer config entry domain so BLE under meross_rpc registers correctly
        domain = coordinator.config_entry.domain if coordinator.config_entry else DOMAIN
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            identifiers={(domain, coordinator.base_unique_id)},
            manufacturer=MANUFACTURER,
            model=MODEL_FRIENDLY_NAME.get(coordinator.model, coordinator.model.value),
            name=coordinator.device_name,
        )
        self._last_logged_ha_state: tuple[bool, Any] | None = None

    @property
    def parsed_data(self) -> dict[str, Any]:
        return self.coordinator.device.parsed_data

    def _entity_log_key(self) -> str:
        desc = getattr(self, "entity_description", None)
        if desc is not None and getattr(desc, "key", None):
            return str(desc.key)
        sensor = getattr(self, "_sensor", None)
        if sensor:
            return str(sensor)
        return type(self).__name__

    def _entity_log_value(self) -> Any:
        native = getattr(type(self), "native_value", None)
        if isinstance(native, property):
            return self.native_value
        is_on = getattr(type(self), "is_on", None)
        if isinstance(is_on, property):
            return self.is_on
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write HA state and INFO-log when the entity value or availability changes."""
        current = (self.available, self._entity_log_value())
        if current != self._last_logged_ha_state:
            LOGGER.info(
                "%s: %s HA entity %s available=%s value=%r",
                self._address,
                self.coordinator.model.value.upper(),
                self._entity_log_key(),
                current[0],
                current[1],
            )
            self._last_logged_ha_state = current
        super()._handle_coordinator_update()
