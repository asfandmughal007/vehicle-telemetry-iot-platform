from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from telemetry_processor.config import DEFAULT_TTL_DAYS, ConfigError, ProcessorConfig
from telemetry_processor.transform import epoch_ms, to_latest_item, to_telemetry_item
from telemetry_processor.validation import MalformedMessageError, parse_event
from tp_helpers import FIXED_NOW, valid_event

# --- validation --------------------------------------------------------------


def test_valid_event_is_parsed_and_topic_key_does_not_leak_into_the_message() -> None:
    message = parse_event(valid_event())

    assert message.vehicle_id == "VH-0001"
    assert message.speed == 87.42
    assert not hasattr(message, "topic_vehicle_id")


@pytest.mark.parametrize("event", [None, "text", 42, ["a", "b"], b"bytes"])
def test_non_object_events_are_rejected(event: object) -> None:
    with pytest.raises(MalformedMessageError) as exc_info:
        parse_event(event)

    assert exc_info.value.reason == "not_a_json_object"


def test_missing_topic_vehicle_id_fails_closed() -> None:
    event = valid_event()
    del event["topic_vehicle_id"]

    with pytest.raises(MalformedMessageError) as exc_info:
        parse_event(event)

    assert exc_info.value.reason == "missing_topic_vehicle_id"


def test_payload_claiming_another_vehicle_than_its_topic_is_rejected() -> None:
    with pytest.raises(MalformedMessageError) as exc_info:
        parse_event(valid_event(topic_vehicle_id="VH-0002"))

    assert exc_info.value.reason == "vehicle_id_mismatch"
    assert "VH-0002" in exc_info.value.details[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("speed", 999.0),
        ("speed", "fast"),
        ("engine_status", "FLYING"),
        ("diagnostic_code", "Z9"),
        ("timestamp", "2026-11-10T09:15:02"),  # naive
        ("message_id", "nope"),
        ("unexpected_field", 1),
    ],
)
def test_schema_violations_are_reported_with_the_offending_field(field: str, value: Any) -> None:
    with pytest.raises(MalformedMessageError) as exc_info:
        parse_event(valid_event(**{field: value}))

    assert exc_info.value.reason == "schema_validation_failed"
    assert any(field in detail for detail in exc_info.value.details)


def test_missing_required_field_is_rejected() -> None:
    event = valid_event()
    del event["rpm"]

    with pytest.raises(MalformedMessageError, match="rpm"):
        parse_event(event)


# --- transform ---------------------------------------------------------------


def contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(contains_float(v) for v in value.values())
    return False


def test_epoch_ms_is_exact_to_the_millisecond() -> None:
    moment = datetime(2026, 11, 10, 9, 15, 2, 100_999, tzinfo=UTC)

    expected = int(datetime(2026, 11, 10, 9, 15, 2, tzinfo=UTC).timestamp()) * 1000 + 100

    assert epoch_ms(moment) == expected  # sub-millisecond part is truncated, not rounded


def test_telemetry_item_has_keys_decimal_numbers_and_ttl() -> None:
    item = to_telemetry_item(parse_event(valid_event()), now=FIXED_NOW, ttl_days=30)

    assert item["vehicle_id"] == "VH-0001"
    assert item["ts"] == epoch_ms(datetime(2026, 11, 10, 9, 15, 2, 100_000, tzinfo=UTC))
    assert item["speed"] == Decimal("87.42")
    assert item["battery_voltage"] == Decimal("14.112")
    assert item["engine_status"] == "RUNNING"
    assert item["device"] == {
        "gateway_id": "gw-VH-0001",
        "firmware_version": "0.1.0",
        "sequence": 1532,
    }
    assert item["expires_at"] == int((FIXED_NOW + timedelta(days=30)).timestamp())
    assert item["timestamp"] == "2026-11-10T09:15:02.100000+00:00"
    assert not contains_float(item)  # DynamoDB rejects Python floats


def test_absent_dtc_is_omitted_and_present_dtc_is_stored() -> None:
    without = to_telemetry_item(parse_event(valid_event()), now=FIXED_NOW, ttl_days=1)
    with_dtc = to_telemetry_item(
        parse_event(valid_event(diagnostic_code="P0217")), now=FIXED_NOW, ttl_days=1
    )

    assert "diagnostic_code" not in without
    assert with_dtc["diagnostic_code"] == "P0217"


def test_latest_item_carries_state_and_active_dtc() -> None:
    plain = to_latest_item(parse_event(valid_event()), now=FIXED_NOW)
    faulty = to_latest_item(parse_event(valid_event(diagnostic_code="P0300")), now=FIXED_NOW)

    assert plain["last_ts"] == epoch_ms(datetime(2026, 11, 10, 9, 15, 2, 100_000, tzinfo=UTC))
    assert plain["latest"]["rpm"] == Decimal("2150.25")
    assert "active_dtc" not in plain
    assert faulty["active_dtc"] == "P0300"
    assert not contains_float(plain)


# --- configuration -----------------------------------------------------------


def test_config_reads_environment() -> None:
    config = ProcessorConfig.from_env(
        {
            "TELEMETRY_TABLE": "t",
            "VEHICLES_TABLE": "v",
            "DLQ_URL": "https://sqs/q",
            "TTL_DAYS": "7",
            "DYNAMODB_ENDPOINT_URL": "http://localhost:8000",
        }
    )

    assert (config.telemetry_table, config.vehicles_table) == ("t", "v")
    assert config.dlq_url == "https://sqs/q"
    assert config.ttl_days == 7
    assert config.endpoint_url == "http://localhost:8000"


def test_config_defaults_for_optional_values() -> None:
    config = ProcessorConfig.from_env(
        {"TELEMETRY_TABLE": "t", "VEHICLES_TABLE": "v", "DLQ_URL": ""}
    )

    assert config.dlq_url is None
    assert config.endpoint_url is None
    assert config.ttl_days == DEFAULT_TTL_DAYS


@pytest.mark.parametrize(
    "env",
    [
        {"VEHICLES_TABLE": "v"},
        {"TELEMETRY_TABLE": "t"},
        {"TELEMETRY_TABLE": " ", "VEHICLES_TABLE": "v"},
        {"TELEMETRY_TABLE": "t", "VEHICLES_TABLE": "v", "TTL_DAYS": "abc"},
        {"TELEMETRY_TABLE": "t", "VEHICLES_TABLE": "v", "TTL_DAYS": "0"},
    ],
)
def test_invalid_config_raises(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        ProcessorConfig.from_env(env)
