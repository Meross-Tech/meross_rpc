"""Psychrometric helpers for MS120 derived sensors (ms120.md).

Absolute humidity, dew point and VPD are not advertised; they are computed
from temperature (°C) and relative humidity (%).
"""

from __future__ import annotations

import math


def _saturation_vapor_pressure_hpa(temp_c: float) -> float:
    """Magnus formula; result in hPa."""
    return 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))


def dew_point_celsius(temp_c: float, humidity: float) -> float | None:
    """Dew point in °C, or None if humidity is out of range."""
    if humidity <= 0 or humidity > 100:
        return None
    gamma = math.log(humidity / 100.0) + (17.67 * temp_c) / (temp_c + 243.5)
    return round((243.5 * gamma) / (17.67 - gamma), 2)


def absolute_humidity_gm3(temp_c: float, humidity: float) -> float | None:
    """Absolute humidity in g/m³."""
    if humidity < 0 or humidity > 100:
        return None
    vapor_hpa = _saturation_vapor_pressure_hpa(temp_c) * (humidity / 100.0)
    return round((216.7 * vapor_hpa) / (temp_c + 273.15), 2)


def vapor_pressure_deficit_kpa(temp_c: float, humidity: float) -> float | None:
    """Vapor pressure deficit in kPa."""
    if humidity < 0 or humidity > 100:
        return None
    es_kpa = _saturation_vapor_pressure_hpa(temp_c) / 10.0
    return round(es_kpa * (1.0 - humidity / 100.0), 3)
