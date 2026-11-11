"""Application logic: maps stored items to API models and implements pagination."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from telemetry_api.cursor import decode_cursor, encode_cursor
from telemetry_api.errors import InvalidQueryError, VehicleNotFoundError
from telemetry_api.models import TelemetryPage, TelemetryRecord, VehicleDetail, VehicleSummary
from telemetry_api.repository import VehicleRepository

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _epoch_ms(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _parse_time(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def telemetry_record(item: dict[str, Any]) -> TelemetryRecord:
    """Map an item of the ``telemetry`` table."""
    return TelemetryRecord.model_validate(
        {
            "vehicle_id": item["vehicle_id"],
            "timestamp": item["timestamp"],
            "message_id": item["message_id"],
            "speed": item["speed"],
            "rpm": item["rpm"],
            "coolant_temperature": item["coolant_temperature"],
            "battery_voltage": item["battery_voltage"],
            "fuel_level": item["fuel_level"],
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "engine_status": item["engine_status"],
            "diagnostic_code": item.get("diagnostic_code"),
        }
    )


def latest_record(vehicle: dict[str, Any]) -> TelemetryRecord:
    """Map the ``latest`` snapshot stored in an item of the ``vehicles`` table."""
    return TelemetryRecord.model_validate(
        {
            **vehicle["latest"],
            "vehicle_id": vehicle["vehicle_id"],
            "timestamp": vehicle["last_seen"],
            "message_id": vehicle["message_id"],
            "diagnostic_code": vehicle.get("active_dtc"),
        }
    )


class FleetService:
    def __init__(
        self,
        repository: VehicleRepository,
        *,
        online_threshold_s: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._online_threshold = timedelta(seconds=online_threshold_s)
        self._clock = clock

    # ------------------------------------------------------------------ vehicles

    def list_vehicles(self) -> list[VehicleSummary]:
        summaries = [self._summary(v) for v in self._repository.list_vehicles()]
        return sorted(summaries, key=lambda s: s.vehicle_id)

    def get_vehicle(self, vehicle_id: str) -> VehicleDetail:
        vehicle = self._require_vehicle(vehicle_id)
        return VehicleDetail(
            **self._summary(vehicle).model_dump(),
            gateway_id=vehicle["gateway_id"],
            latest=latest_record(vehicle),
        )

    def latest(self, vehicle_id: str) -> TelemetryRecord:
        return latest_record(self._require_vehicle(vehicle_id))

    # ----------------------------------------------------------------- telemetry

    def telemetry_page(
        self,
        vehicle_id: str,
        *,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        cursor: str | None,
        newest_first: bool,
    ) -> TelemetryPage:
        start_ms = _epoch_ms(start) if start else None
        end_ms = _epoch_ms(end) if end else None
        if start_ms is not None and end_ms is not None and start_ms > end_ms:
            raise InvalidQueryError("start must not be after end")

        if cursor is not None:
            # Continue strictly after the last item of the previous page. Timestamps are
            # integer milliseconds, so "strictly after" is exactly +/- 1.
            last_ts = decode_cursor(cursor)
            if newest_first:
                end_ms = last_ts - 1 if end_ms is None else min(end_ms, last_ts - 1)
            else:
                start_ms = last_ts + 1 if start_ms is None else max(start_ms, last_ts + 1)

        window_is_empty = start_ms is not None and end_ms is not None and start_ms > end_ms
        # Ask for one item more than needed: that is how we know whether a next page exists
        # without returning a cursor that leads to an empty page.
        items = (
            []
            if window_is_empty
            else self._repository.query_telemetry(
                vehicle_id,
                start_ms=start_ms,
                end_ms=end_ms,
                limit=limit + 1,
                newest_first=newest_first,
            )
        )
        page = items[:limit]
        next_cursor = encode_cursor(int(page[-1]["ts"])) if len(items) > limit else None

        if not page and cursor is None:
            self._require_vehicle(vehicle_id)  # empty window is fine, unknown vehicle is a 404
        return TelemetryPage(
            vehicle_id=vehicle_id,
            items=[telemetry_record(item) for item in page],
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------- helpers

    def _require_vehicle(self, vehicle_id: str) -> dict[str, Any]:
        vehicle = self._repository.get_vehicle(vehicle_id)
        if vehicle is None:
            raise VehicleNotFoundError(vehicle_id)
        return vehicle

    def _summary(self, vehicle: dict[str, Any]) -> VehicleSummary:
        last_seen = _parse_time(vehicle["last_seen"])
        return VehicleSummary(
            vehicle_id=vehicle["vehicle_id"],
            last_seen=last_seen,
            online=self._clock() - last_seen <= self._online_threshold,
            engine_status=vehicle["engine_status"],
            active_dtc=vehicle.get("active_dtc"),
        )
