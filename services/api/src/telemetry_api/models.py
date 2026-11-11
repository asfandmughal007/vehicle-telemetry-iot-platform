"""Response models -- the public contract of the API (shown in the OpenAPI docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from telemetry_common.schema import EngineStatus


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class TelemetryRecord(BaseModel):
    """One telemetry snapshot (units: km/h, 1/min, °C, V, %, WGS84 degrees)."""

    vehicle_id: str
    timestamp: datetime = Field(description="Sampling time at the vehicle gateway (UTC).")
    message_id: str
    speed: float
    rpm: float
    coolant_temperature: float
    battery_voltage: float
    fuel_level: float
    latitude: float
    longitude: float
    engine_status: EngineStatus
    diagnostic_code: str | None = Field(default=None, description="Active DTC or null.")


class VehicleSummary(BaseModel):
    vehicle_id: str
    last_seen: datetime
    online: bool = Field(description="True if telemetry arrived within the online threshold.")
    engine_status: EngineStatus
    active_dtc: str | None = None


class VehicleDetail(VehicleSummary):
    gateway_id: str
    latest: TelemetryRecord


class TelemetryPage(BaseModel):
    vehicle_id: str
    items: list[TelemetryRecord]
    next_cursor: str | None = Field(
        default=None,
        description="Pass as `cursor` to fetch the next page; null on the last page.",
    )
