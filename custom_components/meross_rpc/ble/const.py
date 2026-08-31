"""Constants and protocol contract for Meross HA BLE (ble_ha.md)."""

from __future__ import annotations

from enum import StrEnum
from logging import Logger, getLogger

from ..const import DOMAIN

MANUFACTURER = "Meross"
LOGGER: Logger = getLogger(__package__)

DEFAULT_RETRY_COUNT = 3
CONF_RETRY_COUNT = "retry_count"
CONF_MODEL = "model"

DEVICE_STARTUP_TIMEOUT = 30
# Local watchdog: mark unavailable if no parseable advertisement.
# Needed on macOS where Bleak's discovered cache often never expires, so HA's
# async_track_unavailable may never fire after battery removal / BT off.
ADVERTISEMENT_STALE_SECONDS = 600
# Shared across all Meross BLE entries: Pi/USB adapters often have 1 connection slot.
DATA_BLE_GATT_LOCK = "ble_gatt_lock"
# After a failed GATT attempt, wait for the next advertisement before retrying
# (device is more likely connectable right after it wakes to advertise).
# Idle ads are often ~30s apart; keep the wait below a full minute for UI feel.
GATT_ADV_WAIT_TIMEOUT = 35.0
# If we heard the device this recently, skip waiting and connect immediately.
GATT_FRESH_ADV_SECONDS = 5.0
# BlueZ needs a moment to clear InProgress / free the slot before another connect.
GATT_INPROGRESS_COOLDOWN = 2.0

# ---------------------------------------------------------------------------
# Discovery / advertisement (ble_ha.md)
# ---------------------------------------------------------------------------
# 16-bit Service Data UUID 0x30be (on-air byte order: 30 BE)
MEROSS_SERVICE_DATA_UUID = "0000be30-0000-1000-8000-00805f9b34fb"

# App GATT reused by HA
MEROSS_GATT_SERVICE = "99e7be30-0001-4c6b-98a2-70fcb3471a72"
MEROSS_CHAR_WRITE = "99e7be30-0002-4c6b-98a2-70fcb3471a72"
MEROSS_CHAR_NOTIFY = "99e7be30-0003-4c6b-98a2-70fcb3471a72"

PROTOCOL_VER = 0x01
FIXED_HEADER_LEN = 8
BATTERY_UNAVAILABLE = 0xFF
BATTERY_LOW_THRESHOLD = 20

# Local name in Scan Response: Meross-<MODEL>-<MAC4>
LOCAL_NAME_PREFIX = "Meross-"

# ---------------------------------------------------------------------------
# MEROSS_SUB_DEV_TYPE (broadcast byte 1)
# ---------------------------------------------------------------------------
SUBDEV_MS220 = 0xD0
SUBDEV_MS120 = 0xD1
SUBDEV_MS420 = 0xE0
SUBDEV_MS700 = 0xF0

# Meross frame
FRAME_HEAD = bytes([0x55, 0xAA])
FRAME_TAIL = bytes([0xAA, 0x55])
TRIGGER_SRC_HA_BLE = 0x13

TAG_SPECIAL = 0x01
TAG_ACK = 0x03
TAG_HISTORY_DELETE = 0x07
TAG_TEMP_HISTORY_COUNT = 0x46
TAG_TEMP_HISTORY_DATA = 0x47
TAG_HUMI_HISTORY_COUNT = 0x48
TAG_HUMI_HISTORY_DATA = 0x49
SPECIAL_IDENTIFY = 0x05
SPECIAL_HEARTBEAT = 0x06
ACK_SUCCESS = 0x00

# MS120 history (tem_hum_query.md)
HISTORY_PAGE_SIZE = 50
HISTORY_RECORD_LEN = 8
CONF_TEMP_HISTORY_NEXT_IDX = "temp_history_next_idx"
CONF_HUMI_HISTORY_NEXT_IDX = "humidity_history_next_idx"
CONF_TEMP_HISTORY_LAST_TS = "temp_history_last_ts"
CONF_HUMI_HISTORY_LAST_TS = "humidity_history_last_ts"
# Set after first successful Identify on add/bind (not resent on reload).
CONF_BOUND_IDENTIFY_DONE = "bound_identify_done"


class MerossModel(StrEnum):
    """Internal model; stored in Config Entry data.model."""

    MS220 = "ms220"
    MS120 = "ms120"
    MS420 = "ms420"
    MS700 = "ms700"


# Need GATT for control / identify
CONNECTABLE_MODELS = {
    MerossModel.MS220,
    MerossModel.MS420,
    MerossModel.MS700,
}

# State mainly from advertisements (Identify may still use GATT)
PASSIVE_MODELS = {
    MerossModel.MS120,
    MerossModel.MS220,
    MerossModel.MS420,
    MerossModel.MS700,
}

# MS220 status / alarm bits (ms220.md)
MS220_STATUS_DOOR_OPEN = 0x01
# Legacy firmware put vibration here; new firmware uses alarm_status bit2.
MS220_STATUS_VIBRATION = 0x02
MS220_ALARM_DOOR_OPEN_LONG = 0x01
MS220_ALARM_DOOR_CLOSED_LONG = 0x02
MS220_ALARM_VIBRATION = 0x04
# product_data alarm_enable_map mirrors alarm_status (ms220.md)
MS220_ALARM_ENABLE_DOOR_OPEN_LONG = 0x01
MS220_ALARM_ENABLE_DOOR_CLOSED_LONG = 0x02
MS220_ALARM_ENABLE_VIBRATION = 0x04
MS220_ALARM_ENABLE_ALL = 0x07

MS220_ALARM_ENABLE_KEY_BY_SENSOR: dict[str, str] = {
    "alarm_door_open_long": "alarm_enable_door_open_long",
    "alarm_door_closed_long": "alarm_enable_door_closed_long",
    "alarm_vibration": "alarm_enable_vibration",
}


def ms220_alarm_feature_enabled(sensor_key: str, data: dict) -> bool | None:
    """Whether an optional MS220 alarm is enabled in the Meross app.

    Returns None when firmware does not advertise alarm_enable_map (legacy).
    """
    if "alarm_enable_map" not in data:
        return None
    enable_key = MS220_ALARM_ENABLE_KEY_BY_SENSOR.get(sensor_key)
    if enable_key is None:
        return None
    return data.get(enable_key) is True

# MS220 report_event codes (doorbell / big button; door uses status)
MS220_EVENT_DOORBELL = 0x06
MS220_EVENT_BUTTON_SINGLE = 0x07
MS220_EVENT_BUTTON_DOUBLE = 0x08

# MS420 status bits (ms420.md) — rain / water leak / freeze
MS420_STATUS_RAIN = 0x01  # top droplet / rain
MS420_STATUS_WATER_LEAK = 0x02  # bottom standing water / level
MS420_STATUS_FREEZE = 0x04  # low temperature / freeze risk

# MS700: report_event packs screen (1–3) + button (1–3) → logical buttons 1–9
MS700_BUTTON_COUNT = 9
MS700_SCREEN_COUNT = 3
MS700_BUTTONS_PER_SCREEN = 3
# product_data screen_enable: bit0=screen1, bit1=screen2, bit2=screen3
MS700_SCREEN_ENABLE_ALL = 0x07


def ms700_logical_button(event_code: int) -> int | None:
    """Map MS700 report_event byte to logical button 1–9 (ms700.md)."""
    if event_code & 0xF0:
        return None
    button_id = event_code & 0x03
    screen_id = (event_code >> 2) & 0x03
    if button_id < 1 or button_id > 3 or screen_id < 1 or screen_id > 3:
        return None
    return (screen_id - 1) * 3 + button_id


def ms700_screen_for_button(button_number: int) -> int:
    """Logical button 1–9 → screen_id 1–3."""
    return (button_number - 1) // MS700_BUTTONS_PER_SCREEN + 1


def ms700_button_on_screen(button_number: int) -> int:
    """Logical button 1–9 → button index on its screen (1–3)."""
    return (button_number - 1) % MS700_BUTTONS_PER_SCREEN + 1


def ms700_default_button_name(button_number: int) -> str:
    """Fallback name when the device/app has not set a custom label."""
    return (
        f"screen{ms700_screen_for_button(button_number)}"
        f"-button{ms700_button_on_screen(button_number)}"
    )


def ms700_button_enabled(button_number: int, screen_enable: int) -> bool:
    """Whether logical button is on an enabled screen (ms700.md screen_enable)."""
    screen_id = ms700_screen_for_button(button_number)
    return bool(screen_enable & (1 << (screen_id - 1)))


SUBDEV_TO_MODEL: dict[int, MerossModel] = {
    SUBDEV_MS220: MerossModel.MS220,
    SUBDEV_MS120: MerossModel.MS120,
    SUBDEV_MS420: MerossModel.MS420,
    SUBDEV_MS700: MerossModel.MS700,
}

MODEL_FRIENDLY_NAME: dict[MerossModel, str] = {
    MerossModel.MS220: "Meross MS220",
    MerossModel.MS120: "Meross MS120",
    MerossModel.MS420: "Meross MS420",
    MerossModel.MS700: "Meross MS700",
}

MODEL_TO_SUBDEV: dict[MerossModel, int] = {
    model: subdev for subdev, model in SUBDEV_TO_MODEL.items()
}
