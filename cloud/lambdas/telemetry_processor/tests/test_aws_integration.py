"""Processing against moto-backed DynamoDB and SQS."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError

from telemetry_processor import handler
from telemetry_processor.config import ConfigError
from telemetry_processor.dlq import DeadLetterQueue
from telemetry_processor.local_ingestor import handle_message
from telemetry_processor.processor import Outcome, TelemetryProcessor
from telemetry_processor.repository import TelemetryRepository
from telemetry_processor.transform import epoch_ms
from telemetry_processor.validation import parse_event
from tp_helpers import FIXED_NOW, TELEMETRY_TABLE, VEHICLES_TABLE, AwsFixture, valid_event

# --- repository --------------------------------------------------------------


def test_put_telemetry_is_idempotent(aws: AwsFixture) -> None:
    repo = aws.repository()
    item = {"vehicle_id": "VH-0001", "ts": 1000, "speed": Decimal("1.5")}

    assert repo.put_telemetry(item) is True
    assert repo.put_telemetry({**item, "speed": Decimal("9.9")}) is False  # redelivery

    stored = aws.telemetry.get_item(Key={"vehicle_id": "VH-0001", "ts": 1000})["Item"]
    assert stored["speed"] == Decimal("1.5")  # first write wins


def test_put_latest_keeps_the_newest_state(aws: AwsFixture) -> None:
    repo = aws.repository()

    assert repo.put_latest({"vehicle_id": "VH-1", "last_ts": 2000, "engine_status": "A"}) is True
    assert repo.put_latest({"vehicle_id": "VH-1", "last_ts": 3000, "engine_status": "B"}) is True
    assert repo.put_latest({"vehicle_id": "VH-1", "last_ts": 1000, "engine_status": "OLD"}) is False
    assert (
        repo.put_latest({"vehicle_id": "VH-1", "last_ts": 3000, "engine_status": "SAME"}) is False
    )

    stored = aws.vehicles.get_item(Key={"vehicle_id": "VH-1"})["Item"]
    assert (stored["last_ts"], stored["engine_status"]) == (3000, "B")


def test_repository_propagates_non_conditional_errors() -> None:
    from moto import mock_aws

    with mock_aws():
        import boto3

        repo = TelemetryRepository(boto3.resource("dynamodb"), "missing", "missing")
        with pytest.raises(ClientError) as exc_info:
            repo.put_telemetry({"vehicle_id": "x", "ts": 1})

    assert exc_info.value.response["Error"]["Code"] == "ResourceNotFoundException"


# --- processor ---------------------------------------------------------------


def test_valid_message_is_stored_in_both_tables(aws: AwsFixture) -> None:
    outcome = aws.processor().process(valid_event(diagnostic_code="P0217"))

    assert outcome is Outcome.STORED
    ts = epoch_ms(parse_event(valid_event()).timestamp)
    item = aws.telemetry.get_item(Key={"vehicle_id": "VH-0001", "ts": ts})["Item"]
    assert item["speed"] == Decimal("87.42")
    assert item["diagnostic_code"] == "P0217"
    assert item["received_at"] == FIXED_NOW.isoformat()
    latest = aws.vehicles.get_item(Key={"vehicle_id": "VH-0001"})["Item"]
    assert latest["active_dtc"] == "P0217"
    assert latest["latest"]["rpm"] == Decimal("2150.25")
    assert aws.dlq_messages() == []


def test_redelivered_message_is_a_duplicate_not_a_second_row(aws: AwsFixture) -> None:
    processor = aws.processor()

    assert processor.process(valid_event()) is Outcome.STORED
    assert processor.process(valid_event()) is Outcome.DUPLICATE

    assert aws.telemetry_count() == 1


def test_replayed_old_message_is_stored_but_does_not_overwrite_newer_latest_state(
    aws: AwsFixture,
) -> None:
    processor = aws.processor()
    newer = valid_event(
        message_id="11111111-1111-4111-8111-111111111111",
        timestamp="2026-11-10T09:15:10+00:00",
        speed=100.0,
    )
    older = valid_event(
        message_id="22222222-2222-4222-8222-222222222222",
        timestamp="2026-11-10T09:14:00+00:00",
        speed=10.0,
    )

    processor.process(newer)
    assert processor.process(older) is Outcome.STORED  # history is complete...

    assert aws.telemetry_count() == 2
    latest = aws.vehicles.get_item(Key={"vehicle_id": "VH-0001"})["Item"]
    assert latest["latest"]["speed"] == Decimal("100.0")  # ...latest state stays current


def test_latest_state_is_repaired_when_a_previous_attempt_failed_halfway(aws: AwsFixture) -> None:
    event = valid_event()
    message = parse_event(event)
    from telemetry_processor.transform import to_telemetry_item

    # Simulate: telemetry written, then the function crashed before the vehicles write.
    aws.repository().put_telemetry(to_telemetry_item(message, now=FIXED_NOW, ttl_days=30))

    assert aws.processor().process(event) is Outcome.DUPLICATE

    assert "Item" in aws.vehicles.get_item(Key={"vehicle_id": "VH-0001"})


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (valid_event(speed=999), "schema_validation_failed"),
        (valid_event(topic_vehicle_id="VH-0002"), "vehicle_id_mismatch"),
        ("not an object", "not_a_json_object"),
    ],
)
def test_malformed_message_goes_to_dlq_and_is_not_stored(
    aws: AwsFixture, event: Any, reason: str
) -> None:
    outcome = aws.processor().process(event)

    assert outcome is Outcome.REJECTED
    assert aws.telemetry_count() == 0
    (body,) = aws.dlq_messages()
    record = json.loads(body)
    assert record["reason"] == reason
    assert json.loads(record["original"]) == event


def test_malformed_message_without_configured_dlq_is_still_handled(aws: AwsFixture) -> None:
    assert aws.processor(with_dlq=False).process({"junk": True}) is Outcome.REJECTED


def test_unavailable_dlq_does_not_turn_a_bad_message_into_a_crash(aws: AwsFixture) -> None:
    broken = DeadLetterQueue(aws.sqs, aws.queue_url + "-does-not-exist")
    processor = TelemetryProcessor(aws.repository(), broken, ttl_days=1, clock=lambda: FIXED_NOW)

    assert processor.process({"junk": True}) is Outcome.REJECTED


def test_infrastructure_errors_propagate_for_retry() -> None:
    from moto import mock_aws

    with mock_aws():
        import boto3

        repo = TelemetryRepository(boto3.resource("dynamodb"), "missing", "missing")
        processor = TelemetryProcessor(repo, None, ttl_days=1, clock=lambda: FIXED_NOW)

        with pytest.raises(ClientError):
            processor.process(valid_event())


def test_processing_emits_emf_metrics(aws: AwsFixture, capsys: pytest.CaptureFixture[str]) -> None:
    processor = aws.processor()
    processor.process(valid_event())
    processor.process(valid_event())
    processor.process({"junk": True})

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "_aws" in line]
    names = [m["Name"] for r in records for m in r["_aws"]["CloudWatchMetrics"][0]["Metrics"]]

    assert names.count("MessagesStored") == 1
    assert names.count("DuplicateMessages") == 1
    assert names.count("MalformedMessages") == 1
    assert names.count("ProcessingLatencyMs") == 2
    ingest_lag = next(r for r in records if "IngestLagMs" in r)
    assert ingest_lag["IngestLagMs"] == pytest.approx(900.0)  # 09:15:03 - 09:15:02.1
    assert records[0]["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "VehicleTelemetry"


# --- Lambda handler ----------------------------------------------------------


@pytest.fixture
def lambda_env(monkeypatch: pytest.MonkeyPatch, aws: AwsFixture) -> AwsFixture:
    monkeypatch.setenv("TELEMETRY_TABLE", TELEMETRY_TABLE)
    monkeypatch.setenv("VEHICLES_TABLE", VEHICLES_TABLE)
    monkeypatch.setenv("DLQ_URL", aws.queue_url)
    monkeypatch.setattr(handler, "_processor", None)
    return aws


def test_lambda_handler_end_to_end(lambda_env: AwsFixture) -> None:
    assert handler.lambda_handler(valid_event(), None) == {"outcome": "stored"}
    assert handler.lambda_handler(valid_event(), None) == {"outcome": "duplicate"}
    assert handler.lambda_handler({"bad": "event"}, None) == {"outcome": "rejected"}

    assert lambda_env.telemetry_count() == 1
    assert len(lambda_env.dlq_messages()) == 1


def test_lambda_handler_reuses_its_processor_between_invocations(lambda_env: AwsFixture) -> None:
    handler.lambda_handler(valid_event(), None)
    first = handler._processor

    handler.lambda_handler(valid_event(), None)

    assert handler._processor is first


def test_lambda_handler_fails_loudly_without_configuration(
    monkeypatch: pytest.MonkeyPatch, aws: AwsFixture
) -> None:
    monkeypatch.delenv("TELEMETRY_TABLE", raising=False)
    monkeypatch.setattr(handler, "_processor", None)

    with pytest.raises(ConfigError):
        handler.lambda_handler(valid_event(), None)


# --- local MQTT bridge -------------------------------------------------------


def mqtt_payload(**overrides: Any) -> bytes:
    event = valid_event(**overrides)
    del event["topic_vehicle_id"]  # devices do not send it; the rule adds it
    return json.dumps(event).encode()


def test_bridge_adds_topic_vehicle_id_like_the_iot_rule(aws: AwsFixture) -> None:
    outcome = handle_message(aws.processor(), "vehicles/VH-0001/telemetry", mqtt_payload())

    assert outcome is Outcome.STORED
    assert aws.telemetry_count() == 1


def test_bridge_rejects_publishing_under_another_vehicles_topic(aws: AwsFixture) -> None:
    outcome = handle_message(aws.processor(), "vehicles/VH-0002/telemetry", mqtt_payload())

    assert outcome is Outcome.REJECTED
    assert json.loads(aws.dlq_messages()[0])["reason"] == "vehicle_id_mismatch"


def test_bridge_cannot_be_fooled_by_a_topic_vehicle_id_in_the_payload(aws: AwsFixture) -> None:
    payload = json.loads(mqtt_payload())
    payload["topic_vehicle_id"] = "VH-0001"  # attacker claims the id of the payload
    outcome = handle_message(
        aws.processor(), "vehicles/VH-0002/telemetry", json.dumps(payload).encode()
    )

    assert outcome is Outcome.REJECTED  # the real topic wins


@pytest.mark.parametrize(
    ("topic", "payload", "reason"),
    [
        ("fleet/VH-0001/telemetry", b"{}", "invalid_topic"),
        ("vehicles/VH-0001/status", b"{}", "unexpected_channel"),
        ("vehicles/VH-0001/telemetry", b"\xff\xfe not json", "invalid_json"),
        ("vehicles/VH-0001/telemetry", b"[1, 2]", "not_a_json_object"),
    ],
)
def test_bridge_rejects_garbage_safely(
    aws: AwsFixture, topic: str, payload: bytes, reason: str
) -> None:
    outcome = handle_message(aws.processor(), topic, payload)

    assert outcome is Outcome.REJECTED
    assert json.loads(aws.dlq_messages()[0])["reason"] == reason
    assert aws.telemetry_count() == 0
