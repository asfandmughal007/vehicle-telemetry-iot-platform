"""DynamoDB access with idempotent, order-safe writes."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

_CONDITIONAL_FAILED = "ConditionalCheckFailedException"


def _is_condition_failure(exc: ClientError) -> bool:
    return bool(exc.response.get("Error", {}).get("Code") == _CONDITIONAL_FAILED)


class TelemetryRepository:
    """Writes telemetry and latest-state items.

    Only *conditional check* failures are interpreted (as "already stored" / "out of
    order"). Every other error -- throttling, missing table, permissions -- propagates so
    that Lambda's retry mechanism and the DLQ take over instead of silently losing data.
    """

    def __init__(self, dynamodb: Any, telemetry_table: str, vehicles_table: str) -> None:
        self._telemetry = dynamodb.Table(telemetry_table)
        self._vehicles = dynamodb.Table(vehicles_table)

    def put_telemetry(self, item: dict[str, Any]) -> bool:
        """Store a telemetry item. Returns ``False`` if ``(vehicle_id, ts)`` already exists.

        MQTT QoS 1 is at-least-once, so duplicates are normal (gateway retry after a lost
        PUBACK, IoT rule retry). The key is the message's sampling time, which is
        identical in every redelivery of the same message.
        """
        try:
            self._telemetry.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(vehicle_id) AND attribute_not_exists(ts)",
            )
        except ClientError as exc:
            if _is_condition_failure(exc):
                return False
            raise
        return True

    def put_latest(self, item: dict[str, Any]) -> bool:
        """Replace the vehicle's latest state unless a newer one is already stored.

        The gateway replays its buffer after an outage, so old messages arrive *after*
        newer ones. The condition keeps the latest state monotonic in sampling time.
        Returns ``False`` if the item was older than (or equal to) the stored state.
        """
        try:
            self._vehicles.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(vehicle_id) OR last_ts < :ts",
                ExpressionAttributeValues={":ts": item["last_ts"]},
            )
        except ClientError as exc:
            if _is_condition_failure(exc):
                return False
            raise
        return True
