"""Shared test data and fixtures for the API tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
import pytest
from moto import mock_aws

T0 = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=60)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

TELEMETRY_TABLE = "telemetry-test"
VEHICLES_TABLE = "vehicles-test"


def ms(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(milliseconds=1)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def telemetry_item(
    vehicle_id: str, second: int, *, dtc: str | None = None, numeric: type = float
) -> dict[str, Any]:
    """An item of the ``telemetry`` table, sampled ``second`` seconds after T0."""
    item: dict[str, Any] = {
        "vehicle_id": vehicle_id,
        "ts": ms(at(second)),
        "message_id": f"00000000-0000-4000-8000-{second:012d}",
        "timestamp": at(second).isoformat(),
        "speed": numeric(second),
        "rpm": numeric(2000),
        "coolant_temperature": numeric(90),
        "battery_voltage": numeric(14.1) if numeric is float else Decimal("14.1"),
        "fuel_level": numeric(60),
        "latitude": numeric(49.0134) if numeric is float else Decimal("49.0134"),
        "longitude": numeric(12.1016) if numeric is float else Decimal("12.1016"),
        "engine_status": "RUNNING",
    }
    if dtc:
        item["diagnostic_code"] = dtc
    return item


def vehicle_item(
    vehicle_id: str, last_second: int, *, dtc: str | None = None, numeric: type = float
) -> dict[str, Any]:
    """An item of the ``vehicles`` table (latest state)."""
    latest = {
        k: v
        for k, v in telemetry_item(vehicle_id, last_second, dtc=dtc, numeric=numeric).items()
        if k
        in {
            "speed",
            "rpm",
            "coolant_temperature",
            "battery_voltage",
            "fuel_level",
            "latitude",
            "longitude",
            "engine_status",
        }
    }
    item: dict[str, Any] = {
        "vehicle_id": vehicle_id,
        "last_ts": ms(at(last_second)),
        "last_seen": at(last_second).isoformat(),
        "message_id": f"00000000-0000-4000-8000-{last_second:012d}",
        "gateway_id": f"gw-{vehicle_id}",
        "engine_status": "RUNNING",
        "latest": latest,
    }
    if dtc:
        item["active_dtc"] = dtc
    return item


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy credentials/region so tests can never reach a real AWS account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def create_tables(dynamodb: Any) -> None:
    dynamodb.create_table(
        TableName=TELEMETRY_TABLE,
        KeySchema=[
            {"AttributeName": "vehicle_id", "KeyType": "HASH"},
            {"AttributeName": "ts", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "vehicle_id", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=VEHICLES_TABLE,
        KeySchema=[{"AttributeName": "vehicle_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "vehicle_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def dynamodb() -> Iterator[Any]:
    """moto-backed DynamoDB with both tables, seeded with 25 samples for VH-0001."""
    with mock_aws():
        resource = boto3.resource("dynamodb")
        create_tables(resource)
        telemetry = resource.Table(TELEMETRY_TABLE)
        for second in range(25):
            telemetry.put_item(Item=telemetry_item("VH-0001", second, numeric=Decimal))
        telemetry.put_item(Item=telemetry_item("VH-0002", 5, numeric=Decimal))
        vehicles = resource.Table(VEHICLES_TABLE)
        vehicles.put_item(Item=vehicle_item("VH-0001", 24, dtc="P0217", numeric=Decimal))
        vehicles.put_item(Item=vehicle_item("VH-0002", 5, numeric=Decimal))
        yield resource
