"""Constants for the Refoss RPC integration."""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger, getLogger
from typing import Final

DOMAIN: Final = "meross_rpc"

LOGGER: Logger = getLogger(__package__)

# How the device is reached; missing key means legacy Wi-Fi entries.
CONF_CONNECTION: Final = "connection"
CONNECTION_WIFI: Final = "wifi"
CONNECTION_BLUETOOTH: Final = "bluetooth"


def is_bluetooth_connection(data: Mapping[str, object]) -> bool:
    """Return True when the config entry (or candidate data) is BLE."""
    return data.get(CONF_CONNECTION) == CONNECTION_BLUETOOTH


# Check interval for  devices
REFOSS_CHECK_INTERVAL = 60


# Button Click events for devices
EVENT_REFOSS_CLICK: Final = "meross.click"

ATTR_CLICK_TYPE: Final = "click_type"
ATTR_CHANNEL: Final = "channel"
ATTR_DEVICE: Final = "device"
CONF_SUBTYPE: Final = "subtype"

INPUTS_EVENTS_TYPES: Final = {
    "button_down",
    "button_up",
    "button_single_push",
    "button_double_push",
    "button_triple_push",
    "button_long_push",
}

INPUTS_EVENTS_SUBTYPES: Final = {
    "button1": 1,
    "button2": 2,
}

UPTIME_DEVIATION: Final = 5

# Time to wait before reloading entry when device config change
ENTRY_RELOAD_COOLDOWN = 30


OTA_BEGIN = "ota_begin"
OTA_ERROR = "ota_error"
OTA_PROGRESS = "ota_progress"
OTA_SUCCESS = "ota_success"
