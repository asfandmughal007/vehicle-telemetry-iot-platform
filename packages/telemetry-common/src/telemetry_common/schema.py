"""Telemetry message contract (schema version 1.0).

This is the single definition of the JSON payload published on
``vehicles/{vehicle_id}/telemetry``. The edge gateway validates against it before
publishing, the Lambda validates against it before persisting, and the API uses it
for its response models -- so a contract change is a change in exactly one place.
"""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION: Literal["1.0"] = "1.0"

#: Restricting the ID alphabet keeps it safe to embed in MQTT topics (no ``/ + #``),
#: DynamoDB keys and URLs without escaping.
VEHICLE_ID_PATTERN = r"^[A-Za-z0-9_-]{3,64}$"

#: SAE J2012 style code: system letter + four hex digits, e.g. ``P0217``.
DTC_PATTERN = r"^[PCBU][0-9A-F]{4}$"

VehicleId = Annotated[str, StringConstraints(pattern=VEHICLE_ID_PATTERN)]
DiagnosticCode = Annotated[str, StringConstraints(pattern=DTC_PATTERN)]


class EngineStatus(StrEnum):
    """Coarse engine state as reported by the engine control unit."""

    OFF = "OFF"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    FAULT = "FAULT"


class DeviceMetadata(BaseModel):
    """Information about the edge gateway that produced a message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_id: str = Field(min_length=1, max_length=64)
    firmware_version: str = Field(min_length=1, max_length=32)
    sequence: int = Field(ge=0, description="Monotonic per-gateway counter; gaps reveal data loss.")


class ConnectionStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class StatusMessage(BaseModel):
    """Retained message on ``vehicles/{id}/status``; ``offline`` is also the MQTT last will.

    The ``timestamp`` is the creation time of the payload. For the last will that is the
    gateway start-up time, because the broker publishes it later without the gateway.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    vehicle_id: VehicleId
    status: ConnectionStatus
    timestamp: AwareDatetime
    device: DeviceMetadata


class DiagnosticEventType(StrEnum):
    DTC_SET = "DTC_SET"
    DTC_CLEARED = "DTC_CLEARED"


class DiagnosticEvent(BaseModel):
    """Event on ``vehicles/{id}/diagnostics``: a trouble code was set or cleared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    message_id: UUID
    vehicle_id: VehicleId
    timestamp: AwareDatetime
    event: DiagnosticEventType
    diagnostic_code: DiagnosticCode
    device: DeviceMetadata


class TelemetryMessage(BaseModel):
    """One periodic telemetry snapshot of a vehicle.

    The physical ranges are plausibility limits, not the CAN signal ranges: a value
    outside them indicates a decoding or sensor error and the message is rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    message_id: UUID = Field(description="Unique per message; used for idempotent writes.")
    vehicle_id: VehicleId
    timestamp: AwareDatetime = Field(description="Sampling time at the gateway, timezone-aware.")

    speed: float = Field(ge=0, le=300, description="Vehicle speed in km/h.")
    rpm: float = Field(ge=0, le=8000, description="Engine speed in 1/min.")
    coolant_temperature: float = Field(ge=-40, le=150, description="Coolant temperature in °C.")
    battery_voltage: float = Field(ge=0, le=30, description="Board net voltage in V.")
    fuel_level: float = Field(ge=0, le=100, description="Fuel level in percent.")
    latitude: float = Field(ge=-90, le=90, description="WGS84 latitude in degrees.")
    longitude: float = Field(ge=-180, le=180, description="WGS84 longitude in degrees.")
    engine_status: EngineStatus
    diagnostic_code: DiagnosticCode | None = Field(
        default=None, description="Active diagnostic trouble code, or null if none."
    )

    device: DeviceMetadata
