"""Bluetooth device support for Meross RPC."""

from __future__ import annotations

import asyncio

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from ..const import DOMAIN
from .const import (
    CONF_MODEL,
    CONF_RETRY_COUNT,
    DATA_BLE_GATT_LOCK,
    DEFAULT_RETRY_COUNT,
    LOGGER,
    MerossModel,
)
from .coordinator import MerossBLEDataUpdateCoordinator
from .device import create_device


def _async_ble_gatt_lock(hass: HomeAssistant) -> asyncio.Lock:
    """One shared GATT lock for all Meross BLE devices on this HA instance."""
    store = hass.data.setdefault(DOMAIN, {})
    lock = store.get(DATA_BLE_GATT_LOCK)
    if lock is None:
        lock = asyncio.Lock()
        store[DATA_BLE_GATT_LOCK] = lock
    return lock


def _async_remove_legacy_ble_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop retired BLE entities (Identify button, Bluetooth signal)."""
    if entry.unique_id is None:
        return
    registry = er.async_get(hass)
    for domain, unique_suffix in (
        (Platform.BUTTON, "identify"),
        (Platform.SENSOR, "rssi"),
    ):
        entity_id = registry.async_get_entity_id(
            domain, entry.domain, f"{entry.unique_id}-{unique_suffix}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)


PLATFORMS_BY_MODEL: dict[MerossModel, list[Platform]] = {
    MerossModel.MS120: [
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
    ],
    MerossModel.MS220: [
        Platform.BINARY_SENSOR,
        Platform.SENSOR,
        Platform.EVENT,
    ],
    MerossModel.MS420: [
        Platform.BINARY_SENSOR,
        Platform.SENSOR,
    ],
    MerossModel.MS700: [
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.EVENT,
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
    device.bind_runtime(
        hass,
        connectable=connectable,
        gatt_lock=_async_ble_gatt_lock(hass),
        wait_advertisement=coordinator.async_wait_next_advertisement,
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
    _async_remove_legacy_ble_entities(hass, entry)
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
