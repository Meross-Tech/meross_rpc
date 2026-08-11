"""Advertisement parser for Meross HA BLE (ble_ha.md + product appendices).

Common payload (after Service Data UUID 0x30be):

    0  protocol_ver     0x01
    1  model/subdev     MEROSS_SUB_DEV_TYPE
    2  status           bitmap (product-defined)
    3  alarm_status     bitmap (product-defined)
    4  battery          0-100 or 0xFF unavailable
    5  reserved[0]      0x00
    6  reserved[1]      0x00
    7  report_cnt       N instantaneous events
    8..  events          N x [reqId][event]
    then product_data

MS120 (ms120.md): report_cnt=0, product_data at offset 8:
    temperature int16 BE 0.01 C, humidity uint16 BE 0.01 %
    Absolute humidity, dew point and VPD are derived locally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .const import (
    BATTERY_UNAVAILABLE,
    FIXED_HEADER_LEN,
    LOCAL_NAME_PREFIX,
    MEROSS_GATT_SERVICE,
    MEROSS_SERVICE_DATA_UUID,
    MODEL_FRIENDLY_NAME,
    MS220_ALARM_DOOR_CLOSED_LONG,
    MS220_ALARM_DOOR_OPEN_LONG,
    MS220_STATUS_DOOR_OPEN,
    MS220_STATUS_VIBRATION,
    PROTOCOL_VER,
    SUBDEV_TO_MODEL,
    MerossModel,
)
from .psychrometrics import (
    absolute_humidity_gm3,
    dew_point_celsius,
    vapor_pressure_deficit_kpa,
)


def _normalize_uuid(value: str) -> str:
    return value.lower().replace("-", "")


_SERVICE_DATA_KEYS = {
    MEROSS_SERVICE_DATA_UUID.lower(),
    _normalize_uuid(MEROSS_SERVICE_DATA_UUID),
    "be30",
    "0000be30",
}


@dataclass(slots=True)
class MerossAdvertisement:
    """Parsed Meross HA BLE advertisement."""

    address: str
    device: BLEDevice
    rssi: int
    model: MerossModel
    data: dict[str, Any]
    events: list[tuple[int, int]] = field(default_factory=list)

    @property
    def friendly_name(self) -> str:
        return MODEL_FRIENDLY_NAME.get(self.model, "Meross")


def _guess_model_from_name(name: str | None) -> MerossModel | None:
    if not name:
        return None
    upper = name.upper()
    if "MS605" in upper:
        return MerossModel.MS605
    if "MS220" in upper:
        return MerossModel.MS220
    if "MS120" in upper:
        return MerossModel.MS120
    if "MS420" in upper:
        return MerossModel.MS420
    if "MS700" in upper:
        return MerossModel.MS700
    if upper.startswith("MEROSS"):
        return None
    return None


def _extract_service_data(advertisement: AdvertisementData) -> bytes | None:
    if not advertisement.service_data:
        return None
    for key, value in advertisement.service_data.items():
        key_l = str(key).lower()
        if (
            key_l in _SERVICE_DATA_KEYS
            or _normalize_uuid(key_l) in _SERVICE_DATA_KEYS
            or "be30" in key_l.replace("-", "")
        ):
            return bytes(value)
    return None


def _parse_events(payload: bytes, report_cnt: int) -> tuple[list[tuple[int, int]], int]:
    """Return (events, product_data_offset)."""
    events: list[tuple[int, int]] = []
    offset = FIXED_HEADER_LEN
    for _ in range(report_cnt):
        if offset + 2 > len(payload):
            break
        req_id = payload[offset]
        event_code = payload[offset + 1]
        events.append((req_id, event_code))
        offset += 2
    return events, offset


def _parse_temp_humidity_product_data(
    product_data: bytes, data: dict[str, Any]
) -> None:
    """Parse temp/humidity product_data and derive AH / dew point / VPD."""
    if len(product_data) < 4:
        return
    # ms120.md / ms700.md: temp int16 BE 0.01C, humidity uint16 BE 0.01%
    temp_raw = int.from_bytes(product_data[0:2], "big", signed=True)
    humi_raw = int.from_bytes(product_data[2:4], "big", signed=False)
    temperature = round(temp_raw / 100, 2)
    humidity = round(humi_raw / 100, 2)
    data["temperature"] = temperature
    data["humidity"] = humidity
    # Derived locally; device does not advertise these
    data["dew_point"] = dew_point_celsius(temperature, humidity)
    data["absolute_humidity"] = absolute_humidity_gm3(temperature, humidity)
    data["vpd"] = vapor_pressure_deficit_kpa(temperature, humidity)


def _parse_ms700_product_data(product_data: bytes, data: dict[str, Any]) -> None:
    """MS700: temp/humidity + screen_enable bitmap (ms700.md)."""
    _parse_temp_humidity_product_data(product_data, data)
    if len(product_data) < 5:
        return
    # bit0–2: screen 1–3 enable; bit3–7 reserved
    data["screen_enable"] = product_data[4] & 0x07


def _parse_product_data(
    model: MerossModel, product_data: bytes, data: dict[str, Any]
) -> None:
    """Fill model-specific fields from product_data."""
    if model is MerossModel.MS700:
        _parse_ms700_product_data(product_data, data)
    elif model is MerossModel.MS120:
        _parse_temp_humidity_product_data(product_data, data)
    # MS220: no product_data (ms220.md)


def _parse_ms220_status(status: int, alarm_status: int, data: dict[str, Any]) -> None:
    """Map MS220 status/alarm bitmaps (ms220_ha.md)."""
    data["door_open"] = bool(status & MS220_STATUS_DOOR_OPEN)
    data["vibration"] = bool(status & MS220_STATUS_VIBRATION)
    data["alarm_door_open_long"] = bool(alarm_status & MS220_ALARM_DOOR_OPEN_LONG)
    data["alarm_door_closed_long"] = bool(
        alarm_status & MS220_ALARM_DOOR_CLOSED_LONG
    )


def _parse_payload(payload: bytes, model: MerossModel) -> tuple[dict[str, Any], list[tuple[int, int]]]:
    data: dict[str, Any] = {
        "model": model,
        "modelName": model.value,
        "modelFriendlyName": MODEL_FRIENDLY_NAME[model],
    }
    events: list[tuple[int, int]] = []

    if len(payload) < FIXED_HEADER_LEN:
        return data, events

    protocol_ver = payload[0]
    subdev = payload[1]
    status = payload[2]
    alarm_status = payload[3]
    battery = payload[4]
    report_cnt = payload[7]

    data["protocol_ver"] = protocol_ver
    data["subdev_type"] = subdev
    data["status"] = status
    data["alarm_status"] = alarm_status
    if battery == BATTERY_UNAVAILABLE:
        data["battery"] = None
    elif 0 <= battery <= 100:
        data["battery"] = battery
    else:
        data["battery"] = None

    if model == MerossModel.MS220:
        _parse_ms220_status(status, alarm_status, data)
    elif model in (MerossModel.MS605, MerossModel.MS420):
        # Product HA docs TBD — provisional: bit0 as power
        data["isOn"] = bool(status & 0x01)
    # MS700: status/alarm fixed 0x00; buttons via report_event (ms700.md)

    events, pd_offset = _parse_events(payload, report_cnt)
    product_data = payload[pd_offset:]
    if product_data:
        data["product_data"] = product_data.hex()
    _parse_product_data(model, product_data, data)

    return data, events


def parse_advertisement_data(
    device: BLEDevice,
    advertisement: AdvertisementData,
    model_hint: MerossModel | None = None,
) -> MerossAdvertisement | None:
    """Parse advertisement; return None if not a Meross HA BLE device."""
    name = advertisement.local_name or device.name
    payload = _extract_service_data(advertisement)

    model: MerossModel | None = model_hint
    if payload and len(payload) >= 2:
        if payload[0] != PROTOCOL_VER and model is None:
            # Still try to map subdev if version unexpected
            pass
        mapped = SUBDEV_TO_MODEL.get(payload[1])
        if mapped is not None:
            model = mapped

    if model is None:
        model = _guess_model_from_name(name)

    has_gatt = any(
        _normalize_uuid(str(u)) == _normalize_uuid(MEROSS_GATT_SERVICE)
        for u in (advertisement.service_uuids or [])
    )
    has_name = bool(name and name.startswith(LOCAL_NAME_PREFIX))
    has_payload = payload is not None and len(payload) >= FIXED_HEADER_LEN

    if model is None and not has_payload:
        return None
    if not has_payload and not has_gatt and not has_name:
        return None
    if model is None:
        return None

    parsed, events = _parse_payload(payload or b"", model)
    parsed["address"] = device.address
    parsed["rssi"] = advertisement.rssi

    return MerossAdvertisement(
        address=device.address,
        device=device,
        rssi=advertisement.rssi,
        model=model,
        data=parsed,
        events=events,
    )


def is_meross_device(device: BLEDevice, advertisement: AdvertisementData) -> bool:
    """Return True if advertisement looks like a Meross HA BLE device."""
    return parse_advertisement_data(device, advertisement) is not None
