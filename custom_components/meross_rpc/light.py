"""Light entities for Meross (Wi-Fi RPC has none; Bluetooth MS220 night light)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import is_bluetooth_connection
from .coordinator import RefossConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: RefossConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up light entities for a Meross config entry."""
    if is_bluetooth_connection(config_entry.data):
        from .ble.light import async_setup_entry as async_setup_ble_entry

        await async_setup_ble_entry(hass, config_entry, async_add_entities)
