"""Data access: one protocol, two implementations (DynamoDB and in-memory for tests)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol, TypeVar

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from telemetry_api.errors import RepositoryUnavailableError

#: Safety cap for the fleet list (a scan). Beyond this a GSI / pagination is needed.
MAX_VEHICLES = 1000

T = TypeVar("T")


class VehicleRepository(Protocol):
    """Returns raw stored items (dicts); the service maps them to API models."""

    def list_vehicles(self) -> list[dict[str, Any]]: ...

    def get_vehicle(self, vehicle_id: str) -> dict[str, Any] | None: ...

    def query_telemetry(
        self,
        vehicle_id: str,
        *,
        start_ms: int | None,
        end_ms: int | None,
        limit: int,
        newest_first: bool,
    ) -> list[dict[str, Any]]:
        """Items with ``start_ms <= ts <= end_ms`` (bounds optional), at most ``limit``."""
        ...


class InMemoryVehicleRepository:
    """Reference implementation with the same semantics as the DynamoDB one."""

    def __init__(
        self,
        vehicles: Iterable[dict[str, Any]] = (),
        telemetry: Iterable[dict[str, Any]] = (),
    ) -> None:
        self._vehicles = {v["vehicle_id"]: v for v in vehicles}
        self._telemetry = list(telemetry)

    def list_vehicles(self) -> list[dict[str, Any]]:
        return list(self._vehicles.values())[:MAX_VEHICLES]

    def get_vehicle(self, vehicle_id: str) -> dict[str, Any] | None:
        return self._vehicles.get(vehicle_id)

    def query_telemetry(
        self,
        vehicle_id: str,
        *,
        start_ms: int | None,
        end_ms: int | None,
        limit: int,
        newest_first: bool,
    ) -> list[dict[str, Any]]:
        matching = [
            item
            for item in self._telemetry
            if item["vehicle_id"] == vehicle_id
            and (start_ms is None or item["ts"] >= start_ms)
            and (end_ms is None or item["ts"] <= end_ms)
        ]
        matching.sort(key=lambda item: item["ts"], reverse=newest_first)
        return matching[:limit]


class DynamoVehicleRepository:
    """DynamoDB-backed repository.

    Every AWS error is translated to :class:`RepositoryUnavailableError`, so the HTTP layer
    returns 503 without leaking internals (table names, ARNs) to clients.
    """

    def __init__(self, dynamodb: Any, telemetry_table: str, vehicles_table: str) -> None:
        self._telemetry = dynamodb.Table(telemetry_table)
        self._vehicles = dynamodb.Table(vehicles_table)

    @staticmethod
    def _call(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except (ClientError, BotoCoreError) as exc:
            raise RepositoryUnavailableError("data store unavailable") from exc

    def list_vehicles(self) -> list[dict[str, Any]]:
        def scan() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            kwargs: dict[str, Any] = {}
            while len(items) < MAX_VEHICLES:
                response = self._vehicles.scan(**kwargs)
                items.extend(response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    break
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            return items[:MAX_VEHICLES]

        return self._call(scan)

    def get_vehicle(self, vehicle_id: str) -> dict[str, Any] | None:
        item: dict[str, Any] | None = self._call(
            lambda: self._vehicles.get_item(Key={"vehicle_id": vehicle_id}).get("Item")
        )
        return item

    def query_telemetry(
        self,
        vehicle_id: str,
        *,
        start_ms: int | None,
        end_ms: int | None,
        limit: int,
        newest_first: bool,
    ) -> list[dict[str, Any]]:
        condition = Key("vehicle_id").eq(vehicle_id)
        if start_ms is not None and end_ms is not None:
            condition &= Key("ts").between(start_ms, end_ms)
        elif start_ms is not None:
            condition &= Key("ts").gte(start_ms)
        elif end_ms is not None:
            condition &= Key("ts").lte(end_ms)

        def query() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": condition,
                "ScanIndexForward": not newest_first,
            }
            # A single Query may return less than `limit` (1 MB page cap): keep going.
            while len(items) < limit:
                response = self._telemetry.query(Limit=limit - len(items), **kwargs)
                items.extend(response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    break
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            return items

        return self._call(query)
