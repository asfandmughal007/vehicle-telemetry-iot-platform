"""Transform a validated message into DynamoDB items.

* ``telemetry`` table: one item per message, key ``(vehicle_id, ts)``, ``ts`` = epoch
  milliseconds as a number so a time range is a simple ``BETWEEN`` on the sort key.
* ``vehicles`` table: one item per vehicle holding its latest state.

DynamoDB rejects Python floats, so numbers are stored as ``Decimal`` created from their
shortest string form (``Decimal(str(x))``), which avoids binary-float artefacts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from telemetry_common.schema import TelemetryMessage

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SIGNAL_FIELDS = (
    "speed",
    "rpm",
    "coolant_temperature",
    "battery_voltage",
    "fuel_level",
    "latitude",
    "longitude",
)


def epoch_ms(moment: datetime) -> int:
    """Exact epoch milliseconds (integer arithmetic, no float rounding)."""
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _signals(message: TelemetryMessage) -> dict[str, Any]:
    signals: dict[str, Any] = {
        name: Decimal(str(getattr(message, name))) for name in _SIGNAL_FIELDS
    }
    signals["engine_status"] = message.engine_status.value
    if message.diagnostic_code is not None:  # absent attribute = no active DTC
        signals["diagnostic_code"] = message.diagnostic_code
    return signals


def to_telemetry_item(message: TelemetryMessage, *, now: datetime, ttl_days: int) -> dict[str, Any]:
    return {
        "vehicle_id": message.vehicle_id,
        "ts": epoch_ms(message.timestamp),
        "message_id": str(message.message_id),
        "schema_version": message.schema_version,
        "timestamp": message.timestamp.astimezone(UTC).isoformat(),
        "received_at": now.astimezone(UTC).isoformat(),
        **_signals(message),
        "device": {
            "gateway_id": message.device.gateway_id,
            "firmware_version": message.device.firmware_version,
            "sequence": message.device.sequence,
        },
        "expires_at": int((now + timedelta(days=ttl_days)).timestamp()),  # DynamoDB TTL (epoch s)
    }


def to_latest_item(message: TelemetryMessage, *, now: datetime) -> dict[str, Any]:
    item: dict[str, Any] = {
        "vehicle_id": message.vehicle_id,
        "last_ts": epoch_ms(message.timestamp),
        "last_seen": message.timestamp.astimezone(UTC).isoformat(),
        "received_at": now.astimezone(UTC).isoformat(),
        "message_id": str(message.message_id),
        "gateway_id": message.device.gateway_id,
        "engine_status": message.engine_status.value,
        "latest": _signals(message),
    }
    if message.diagnostic_code is not None:
        item["active_dtc"] = message.diagnostic_code
    return item
