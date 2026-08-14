"""Bluetooth device support for Meross RPC."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_MODEL,
    CONF_RETRY_COUNT,
    DEFAULT_RETRY_COUNT,
    LOGGER,
    MerossModel,
)
from .coordinator import MerossBLEDataUpdateCoordinator
from .device import create_device

PLATFORMS_BY_MODEL: dict[MerossModel, list[Platform]] = {
    MerossModel.MS120: [
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
    ],
    MerossModel.MS220: [
        Platform.BINARY_SENSOR,
        Platform.SENSOR,
        Platform.EVENT,
        Platform.BUTTON,
    ],
    MerossModel.MS420: [
        Platform.BINARY_SENSOR,
        Platform.SENSOR,
        Platform.BUTTON,
    ],
    MerossModel.MS700: [
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.EVENT,
        Platform.BUTTON,
    ],
}


async def async_setup_bluetooth_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up one Meross BLE device from a meross_rpc config entry."""
    assert entry.unique_id is not None
    address: str = entry.data[CONF_ADDRESS]
    model = MerossModel(entry.data[CONF_MODEL])
    connectable = True
    retry_count = entry.options.get(CONF_RETRY_COUNT, DEFAULT_RETRY_COUNT)

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address.upper(), connectable
    )
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Meross BLE device with address {address}"
        )

    device = create_device(ble_device, model, retry_count=retry_count)
    coordinator = entry.runtime_data = MerossBLEDataUpdateCoordinator(
        hass,
        LOGGER,
        ble_device,
        device,
        entry.unique_id,
        entry.title,
        connectable,
        model,
        entry,
    )
    entry.async_on_unload(coordinator.async_start())
    if not await coordinator.async_wait_ready():
        raise ConfigEntryNotReady(
            f"Meross BLE device {address} not advertising yet; will retry"
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS_BY_MODEL[model]
    )
    if model is MerossModel.MS120:
        # Setup / reload: ask firmware for anything newer than last import.
        coordinator.history_force_full_resync = True
        coordinator.async_schedule_history_sync()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_bluetooth_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload BLE platforms for a meross_rpc entry."""
    model = MerossModel(entry.data[CONF_MODEL])
    return await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_BY_MODEL[model]
    )
