"""Import MS120 local history into Home Assistant statistics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import logging
from statistics import fmean
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_HUMI_HISTORY_NEXT_IDX,
    CONF_TEMP_HISTORY_NEXT_IDX,
    MerossModel,
)
from .device import MerossBLEError
from .protocol import HistorySample

if TYPE_CHECKING:
    from .coordinator import MerossBLEDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

RECORDER_DOMAIN = "recorder"


def _hourly_statistics(samples: list[HistorySample]) -> list[StatisticData]:
    """Bucket samples into hourly mean/min/max for recorder import."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for sample in samples:
        hour = sample.timestamp.replace(minute=0, second=0, microsecond=0)
        buckets[hour].append(sample.value)
    return [
        StatisticData(
            start=hour,
            mean=round(fmean(values), 2),
            min=round(min(values), 2),
            max=round(max(values), 2),
        )
        for hour, values in sorted(buckets.items())
    ]


def _entity_id(hass: HomeAssistant, platform_domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", platform_domain, unique_id)


async def async_sync_ms120_history(
    hass: HomeAssistant, coordinator: MerossBLEDataUpdateCoordinator
) -> None:
    """Pull missing MS120 history over GATT and import into HA statistics."""
    if coordinator.model is not MerossModel.MS120:
        return

    entry = coordinator.config_entry
    platform_domain = entry.domain
    temp_next = int(entry.data.get(CONF_TEMP_HISTORY_NEXT_IDX, 0))
    humi_next = int(entry.data.get(CONF_HUMI_HISTORY_NEXT_IDX, 0))

    try:
        temp_samples = await coordinator.device.fetch_temperature_history(temp_next)
        humi_samples = await coordinator.device.fetch_humidity_history(humi_next)
    except MerossBLEError as err:
        _LOGGER.warning(
            "%s: MS120 history sync failed: %s", coordinator.ble_device.address, err
        )
        return

    new_data = dict(entry.data)
    updated = False

    temp_entity = _entity_id(
        hass, platform_domain, f"{coordinator.base_unique_id}-temperature"
    )
    if temp_samples and temp_entity:
        async_import_statistics(
            hass,
            {
                "has_sum": False,
                "mean_type": StatisticMeanType.ARITHMETIC,
                "name": None,
                "source": RECORDER_DOMAIN,
                "statistic_id": temp_entity,
                "unit_class": TemperatureConverter.UNIT_CLASS,
                "unit_of_measurement": UnitOfTemperature.CELSIUS,
            },
            _hourly_statistics(temp_samples),
        )
        new_data[CONF_TEMP_HISTORY_NEXT_IDX] = max(s.index for s in temp_samples) + 1
        updated = True
        _LOGGER.info(
            "%s: imported %s temperature history samples into %s",
            coordinator.ble_device.address,
            len(temp_samples),
            temp_entity,
        )
    elif temp_samples:
        _LOGGER.debug(
            "%s: temperature history fetched but entity not registered yet",
            coordinator.ble_device.address,
        )

    humi_entity = _entity_id(
        hass, platform_domain, f"{coordinator.base_unique_id}-humidity"
    )
    if humi_samples and humi_entity:
        async_import_statistics(
            hass,
            {
                "has_sum": False,
                "mean_type": StatisticMeanType.ARITHMETIC,
                "name": None,
                "source": RECORDER_DOMAIN,
                "statistic_id": humi_entity,
                "unit_class": None,
                "unit_of_measurement": PERCENTAGE,
            },
            _hourly_statistics(humi_samples),
        )
        new_data[CONF_HUMI_HISTORY_NEXT_IDX] = max(s.index for s in humi_samples) + 1
        updated = True
        _LOGGER.info(
            "%s: imported %s humidity history samples into %s",
            coordinator.ble_device.address,
            len(humi_samples),
            humi_entity,
        )
    elif humi_samples:
        _LOGGER.debug(
            "%s: humidity history fetched but entity not registered yet",
            coordinator.ble_device.address,
        )

    if updated:
        hass.config_entries.async_update_entry(entry, data=new_data)
