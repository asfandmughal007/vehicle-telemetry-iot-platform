from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from telemetry_common.schema import EngineStatus, TelemetryMessage


def valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "message_id": "8f6d1c1e-2a7b-4c59-9a3e-0f6c8f1d2b77",
        "vehicle_id": "VH-0001",
        "timestamp": "2026-11-10T09:15:02.100000+00:00",
        "speed": 87.42,
        "rpm": 2150.25,
        "coolant_temperature": 91.0,
        "battery_voltage": 14.112,
        "fuel_level": 63.2,
        "latitude": 49.0134123,
        "longitude": 12.1016456,
        "engine_status": "RUNNING",
        "diagnostic_code": None,
        "device": {"gateway_id": "gw-VH-0001", "firmware_version": "0.1.0", "sequence": 1532},
    }
    payload.update(overrides)
    return payload


def test_valid_message_is_accepted() -> None:
    message = TelemetryMessage.model_validate(valid_payload())

    assert message.vehicle_id == "VH-0001"
    assert message.engine_status is EngineStatus.RUNNING
    assert message.diagnostic_code is None
    assert message.timestamp.utcoffset() is not None


def test_json_round_trip_preserves_message() -> None:
    message = TelemetryMessage.model_validate(valid_payload(diagnostic_code="P0217"))

    restored = TelemetryMessage.model_validate_json(message.model_dump_json())

    assert restored == message
    assert json.loads(message.model_dump_json())["diagnostic_code"] == "P0217"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("speed", -1.0),
        ("speed", 301.0),
        ("rpm", 9000.0),
        ("coolant_temperature", 200.0),
        ("battery_voltage", -0.1),
        ("fuel_level", 100.5),
        ("latitude", 90.1),
        ("longitude", -180.1),
        ("engine_status", "FLYING"),
        ("diagnostic_code", "X1234"),
        ("diagnostic_code", "P021"),
        ("vehicle_id", "a/b"),
        ("vehicle_id", "vehicles/+"),
        ("schema_version", "2.0"),
        ("message_id", "not-a-uuid"),
    ],
)
def test_out_of_contract_values_are_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(valid_payload(**{field: value}))


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(valid_payload(timestamp="2026-11-10T09:15:02"))


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(valid_payload(unexpected="value"))


def test_missing_required_field_is_rejected() -> None:
    payload = valid_payload()
    del payload["speed"]

    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(payload)
