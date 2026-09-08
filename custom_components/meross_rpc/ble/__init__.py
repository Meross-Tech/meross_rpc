"""Bluetooth device support for Meross RPC."""

from __future__ import annotations

import asyncio
import logging
import time

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

_LOGGER = logging.getLogger(__name__)


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
    # GATT Identify still needs a connectable BLEDevice from the cache.
    gatt_connectable = True
    # False = receive connectable and non-connectable ads. Door/button wake
    # packets are often flagged non-connectable on macOS; True would drop
    # them so Opening never updates after the unavailable watchdog.
    advertisement_connectable = False
    retry_count = entry.options.get(CONF_RETRY_COUNT, DEFAULT_RETRY_COUNT)

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address.upper(), gatt_connectable
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
        advertisement_connectable,
        model,
        entry,
    )
    def _inspect_ble_cache(reason: str) -> None:
        """Dump HA bluetooth-manager cache for GATT debug (host-side)."""
        for connectable in (True, False):
            info = bluetooth.async_last_service_info(
                hass, address, connectable=connectable
            )
            ble = bluetooth.async_ble_device_from_address(
                hass, address.upper(), connectable
            )
            if info is None:
                _LOGGER.info(
                    "%s [CACHE] %s HA last_service_info connectable=%s empty "
                    "ble_device=%s",
                    address,
                    reason,
                    connectable,
                    ble,
                )
                continue
            age = time.monotonic() - info.time
            adv = info.advertisement
            service_data = {
                str(key): bytes(value).hex()
                for key, value in (adv.service_data or {}).items()
            }
            _LOGGER.info(
                "%s [CACHE] %s HA last_service_info connectable=%s age=%.1fs "
                "name=%r rssi=%s adv_connectable=%s service_uuids=%s "
                "service_data=%s ble_device_name=%r",
                address,
                reason,
                connectable,
                age,
                info.name,
                info.rssi,
                info.connectable,
                list(adv.service_uuids or []),
                service_data or "(none)",
                None if ble is None else ble.name,
            )

    device.bind_runtime(
        refresh_ble_device=lambda: bluetooth.async_ble_device_from_address(
            hass, address.upper(), gatt_connectable
        ),
        gatt_lock=_async_ble_gatt_lock(hass),
        wait_advertisement=coordinator.async_wait_next_advertisement,
        inspect_ble_cache=_inspect_ble_cache,
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
