"""Meross HA BLE frame helpers (ble_ha.md section 3 + tem_hum_query.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .const import (
    ACK_SUCCESS,
    FRAME_HEAD,
    FRAME_TAIL,
    HISTORY_RECORD_LEN,
    SPECIAL_HEARTBEAT,
    SPECIAL_IDENTIFY,
    TAG_ACK,
    TAG_HISTORY_DELETE,
    TAG_HUMI_HISTORY_COUNT,
    TAG_HUMI_HISTORY_DATA,
    TAG_SPECIAL,
    TAG_TEMP_HISTORY_COUNT,
    TAG_TEMP_HISTORY_DATA,
    TRIGGER_SRC_HA_BLE,
)


@dataclass(slots=True, frozen=True)
class HistorySample:
    """One local history sample from MS120."""

    index: int
    timestamp: datetime
    value: float


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly=0x1021, init=0xFFFF, xorout=0."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_tlv(tag: int, value: bytes) -> bytes:
    """Build one TLV: [tag:1][length:2 BE][value]."""
    return bytes([tag]) + len(value).to_bytes(2, "big") + value


def build_meross_frame(subdev_type: int, msg_id: int, tlvs: bytes) -> bytes:
    """Build a full Meross request/response frame."""
    inner = bytes([TRIGGER_SRC_HA_BLE, msg_id & 0xFF]) + tlvs
    payload_len = len(inner)
    body = (
        FRAME_HEAD
        + bytes([subdev_type & 0xFF])
        + payload_len.to_bytes(2, "big")
        + inner
    )
    crc = crc16_ccitt_false(inner)
    return body + crc.to_bytes(2, "big") + FRAME_TAIL


def build_identify_frame(subdev_type: int, msg_id: int = 1) -> bytes:
    """HA -> device Identify (TAG_SPECIAL, 0x05)."""
    tlv = build_tlv(TAG_SPECIAL, bytes([SPECIAL_IDENTIFY]))
    return build_meross_frame(subdev_type, msg_id, tlv)


def build_heartbeat_frame(subdev_type: int, msg_id: int = 1) -> bytes:
    """HA -> device Heartbeat (TAG_SPECIAL, 0x06)."""
    tlv = build_tlv(TAG_SPECIAL, bytes([SPECIAL_HEARTBEAT]))
    return build_meross_frame(subdev_type, msg_id, tlv)


def build_history_count_frame(
    subdev_type: int, tag: int, msg_id: int = 1
) -> bytes:
    """Query history count (0x46 temperature / 0x48 humidity)."""
    return build_meross_frame(subdev_type, msg_id, build_tlv(tag, b""))


def build_history_data_frame(
    subdev_type: int,
    tag: int,
    start_idx: int,
    end_idx: int,
    msg_id: int = 1,
) -> bytes:
    """Query history page (0x47 temperature / 0x49 humidity), max 50 records."""
    value = start_idx.to_bytes(2, "big") + end_idx.to_bytes(2, "big")
    return build_meross_frame(subdev_type, msg_id, build_tlv(tag, value))


def build_history_delete_frame(
    subdev_type: int, history_tag: int, msg_id: int = 1
) -> bytes:
    """Delete history for tag 0x47 (temp) or 0x49 (humidity)."""
    return build_meross_frame(
        subdev_type, msg_id, build_tlv(TAG_HISTORY_DELETE, bytes([history_tag]))
    )


def iter_tlvs(payload: bytes) -> list[tuple[int, bytes]]:
    """Parse TLVs from a full Meross frame body (excluding CRC/tail)."""
    if len(payload) < 11 or payload[:2] != FRAME_HEAD or payload[-2:] != FRAME_TAIL:
        return []
    results: list[tuple[int, bytes]] = []
    idx = 7
    end = len(payload) - 4
    while idx + 3 <= end:
        tag = payload[idx]
        length = int.from_bytes(payload[idx + 1 : idx + 3], "big")
        if idx + 3 + length > end:
            break
        value = payload[idx + 3 : idx + 3 + length]
        results.append((tag, value))
        idx += 3 + length
    return results


def parse_ack_success(payload: bytes) -> bool:
    """Return True if Notify payload looks like a successful TAG_ACK frame."""
    for tag, value in iter_tlvs(payload):
        if tag == TAG_ACK and value == bytes([ACK_SUCCESS]):
            return True
    return False


def parse_history_count(payload: bytes, expected_tag: int) -> int | None:
    """Parse his_num (uint16 BE) from count response."""
    for tag, value in iter_tlvs(payload):
        if tag == expected_tag and len(value) >= 2:
            return int.from_bytes(value[0:2], "big")
    return None


def parse_history_samples(
    payload: bytes, expected_tag: int, *, scale: float
) -> list[HistorySample]:
    """Parse history data TLV into samples.

    Temperature history: int16 BE / 100.
    Humidity history: int16 BE / 10.
    Timestamp: Unix seconds UTC, big-endian.
    """
    samples: list[HistorySample] = []
    for tag, value in iter_tlvs(payload):
        if tag != expected_tag:
            continue
        if len(value) % HISTORY_RECORD_LEN != 0:
            continue
        for offset in range(0, len(value), HISTORY_RECORD_LEN):
            chunk = value[offset : offset + HISTORY_RECORD_LEN]
            index = int.from_bytes(chunk[0:2], "big")
            ts = int.from_bytes(chunk[2:6], "big")
            raw = int.from_bytes(chunk[6:8], "big", signed=True)
            samples.append(
                HistorySample(
                    index=index,
                    timestamp=datetime.fromtimestamp(ts, tz=UTC),
                    value=round(raw / scale, 2 if scale == 100 else 1),
                )
            )
    return samples


# Convenience aliases matching tem_hum_query.md tags
def build_temp_history_count_frame(subdev_type: int, msg_id: int = 1) -> bytes:
    return build_history_count_frame(subdev_type, TAG_TEMP_HISTORY_COUNT, msg_id)


def build_temp_history_data_frame(
    subdev_type: int, start_idx: int, end_idx: int, msg_id: int = 1
) -> bytes:
    return build_history_data_frame(
        subdev_type, TAG_TEMP_HISTORY_DATA, start_idx, end_idx, msg_id
    )


def build_humi_history_count_frame(subdev_type: int, msg_id: int = 1) -> bytes:
    return build_history_count_frame(subdev_type, TAG_HUMI_HISTORY_COUNT, msg_id)


def build_humi_history_data_frame(
    subdev_type: int, start_idx: int, end_idx: int, msg_id: int = 1
) -> bytes:
    return build_history_data_frame(
        subdev_type, TAG_HUMI_HISTORY_DATA, start_idx, end_idx, msg_id
    )
