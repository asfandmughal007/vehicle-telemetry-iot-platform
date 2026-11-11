"""Shared helpers and fixtures for the telemetry processor tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from telemetry_processor.dlq import DeadLetterQueue
from telemetry_processor.processor import TelemetryProcessor
from telemetry_processor.repository import TelemetryRepository
from telemetry_processor.tables import create_tables

TELEMETRY_TABLE = "telemetry-test"
VEHICLES_TABLE = "vehicles-test"
FIXED_NOW = datetime(2026, 11, 10, 9, 15, 3, tzinfo=UTC)


def valid_event(**overrides: Any) -> dict[str, Any]:
    """What the IoT rule delivers: the device payload plus ``topic_vehicle_id``."""
    event: dict[str, Any] = {
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
        "topic_vehicle_id": "VH-0001",
    }
    event.update(overrides)
    return event


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy credentials/region so nothing can ever reach a real AWS account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


class AwsFixture:
    def __init__(self) -> None:
        self.dynamodb = boto3.resource("dynamodb")
        self.sqs = boto3.client("sqs")
        create_tables(self.dynamodb, TELEMETRY_TABLE, VEHICLES_TABLE)
        self.queue_url: str = self.sqs.create_queue(QueueName="dlq-test")["QueueUrl"]
        self.telemetry = self.dynamodb.Table(TELEMETRY_TABLE)
        self.vehicles = self.dynamodb.Table(VEHICLES_TABLE)

    def repository(self) -> TelemetryRepository:
        return TelemetryRepository(self.dynamodb, TELEMETRY_TABLE, VEHICLES_TABLE)

    def processor(self, *, with_dlq: bool = True) -> TelemetryProcessor:
        dlq = DeadLetterQueue(self.sqs, self.queue_url) if with_dlq else None
        return TelemetryProcessor(self.repository(), dlq, ttl_days=30, clock=lambda: FIXED_NOW)

    def dlq_messages(self) -> list[str]:
        response = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10)
        return [m["Body"] for m in response.get("Messages", [])]

    def telemetry_count(self) -> int:
        return int(self.telemetry.scan(Select="COUNT")["Count"])


@pytest.fixture
def aws() -> Iterator[AwsFixture]:
    with mock_aws():
        yield AwsFixture()
