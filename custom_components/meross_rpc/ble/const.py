"""HA-specific BLE constants; protocol constants come from meross_ha."""

from __future__ import annotations

from logging import Logger, getLogger

from meross_ha import (
    BATTERY_LOW_THRESHOLD as BATTERY_LOW_THRESHOLD,
    CONNECTABLE_MODELS as CONNECTABLE_MODELS,
    DEFAULT_RETRY_COUNT as DEFAULT_RETRY_COUNT,
    MODEL_FRIENDLY_NAME as MODEL_FRIENDLY_NAME,
    MS220_EVENT_BUTTON_DOUBLE as MS220_EVENT_BUTTON_DOUBLE,
    MS220_EVENT_BUTTON_SINGLE as MS220_EVENT_BUTTON_SINGLE,
    MS220_EVENT_DOORBELL as MS220_EVENT_DOORBELL,
    MS700_BUTTON_COUNT as MS700_BUTTON_COUNT,
    MerossModel as MerossModel,
    ms220_alarm_feature_enabled as ms220_alarm_feature_enabled,
    ms700_button_enabled as ms700_button_enabled,
    ms700_default_button_name as ms700_default_button_name,
    ms700_logical_button as ms700_logical_button,
)
from meross_ha.const import (
    GATT_ADV_WAIT_TIMEOUT as GATT_ADV_WAIT_TIMEOUT,
    GATT_FRESH_ADV_SECONDS as GATT_FRESH_ADV_SECONDS,
    GATT_INPROGRESS_COOLDOWN as GATT_INPROGRESS_COOLDOWN,
    GATT_NOTIFY_TIMEOUT as GATT_NOTIFY_TIMEOUT,
    GATT_POST_CONNECT_SETTLE as GATT_POST_CONNECT_SETTLE,
    GATT_REDISCOVER_SETTLE as GATT_REDISCOVER_SETTLE,
    MANUFACTURER as MANUFACTURER,
)

from ..const import DOMAIN

LOGGER: Logger = getLogger(__package__)

CONF_RETRY_COUNT = "retry_count"
CONF_MODEL = "model"

DEVICE_STARTUP_TIMEOUT = 30
# Local watchdog: mark unavailable if no parseable advertisement.
ADVERTISEMENT_STALE_SECONDS = 600
# Shared across all Meross BLE entries: Pi/USB adapters often have 1 connection slot.
DATA_BLE_GATT_LOCK = "ble_gatt_lock"

CONF_TEMP_HISTORY_NEXT_IDX = "temp_history_next_idx"
CONF_HUMI_HISTORY_NEXT_IDX = "humidity_history_next_idx"
CONF_TEMP_HISTORY_LAST_TS = "temp_history_last_ts"
CONF_HUMI_HISTORY_LAST_TS = "humidity_history_last_ts"
# Set after first successful Identify on add/bind (not resent on reload).
CONF_BOUND_IDENTIFY_DONE = "bound_identify_done"
