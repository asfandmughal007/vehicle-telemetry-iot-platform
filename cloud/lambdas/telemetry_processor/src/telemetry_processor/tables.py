"""Table definitions for local development and tests.

In AWS the same tables are defined in the CDK stack (phase 7); keep both in sync.
"""

from __future__ import annotations

import contextlib
from typing import Any

from botocore.exceptions import ClientError


def create_tables(dynamodb: Any, telemetry_table: str, vehicles_table: str) -> None:
    """Create the two tables if they do not exist yet (idempotent)."""
    existing = {t.name for t in dynamodb.tables.all()}

    if telemetry_table not in existing:
        dynamodb.create_table(
            TableName=telemetry_table,
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
        dynamodb.Table(telemetry_table).wait_until_exists()
        with contextlib.suppress(ClientError):  # TTL is a nice-to-have for local runs
            dynamodb.meta.client.update_time_to_live(
                TableName=telemetry_table,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
            )

    if vehicles_table not in existing:
        dynamodb.create_table(
            TableName=vehicles_table,
            KeySchema=[{"AttributeName": "vehicle_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "vehicle_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.Table(vehicles_table).wait_until_exists()
