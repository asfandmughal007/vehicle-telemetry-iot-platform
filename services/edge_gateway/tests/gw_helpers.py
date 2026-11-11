"""Shared helpers for the gateway tests (kept independent of the simulator package)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import can

from edge_gateway.buffer import BufferedMessage
from telemetry_common.can_db import FrameName, load_vehicle_dbc
from telemetry_common.dtc import encode_dtc

FIXED_TIME = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)


def make_frame(name: FrameName, signals: dict[str, float | int]) -> can.Message:
    message = load_vehicle_dbc().get_message_by_name(name.value)
    return can.Message(
        arbitration_id=message.frame_id, data=message.encode(signals), is_extended_id=False
    )


def vehicle_frames(
    *,
    speed: float = 50.0,
    rpm: float = 2000.0,
    coolant: float = 90.0,
    voltage: float = 14.1,
    fuel: float = 60.0,
    latitude: float = 49.0134,
    longitude: float = 12.1016,
    engine_status_raw: int = 2,
    dtc: str | None = None,
) -> list[can.Message]:
    category, number = encode_dtc(dtc) if dtc else (0, 0)
    return [
        make_frame(
            FrameName.ENGINE_DATA,
            {"EngineSpeed": rpm, "CoolantTemperature": coolant, "EngineStatus": engine_status_raw},
        ),
        make_frame(FrameName.VEHICLE_DYNAMICS, {"VehicleSpeed": speed, "FuelLevel": fuel}),
        make_frame(FrameName.BATTERY_STATUS, {"BatteryVoltage": voltage}),
        make_frame(FrameName.GNSS_POSITION, {"Latitude": latitude, "Longitude": longitude}),
        make_frame(
            FrameName.DIAGNOSTIC_TROUBLE_CODE,
            {"DtcActive": int(dtc is not None), "DtcCategory": category, "DtcCode": number},
        ),
    ]


class FakeClock:
    """Callable clock whose time only moves when told to (or by ``tick`` per call)."""

    def __init__(self, start: float = 1000.0, tick: float = 0.0) -> None:
        self.now = start
        self._tick = tick

    def __call__(self) -> float:
        self.now += self._tick
        return self.now


class FakeTransport:
    """Scriptable transport: ``results`` are consumed per publish, then ``default`` applies."""

    def __init__(self, *, connected: bool = True, default: bool = True) -> None:
        self._connected = connected
        self.default = default
        self.results: list[bool] = []
        self.published: list[tuple[str, str]] = []
        self.started = False
        self.stopped = False

    @property
    def connected(self) -> bool:
        return self._connected

    def set_connected(self, value: bool) -> None:
        self._connected = value

    def publish(self, topic: str, payload: str, *, timeout_s: float) -> bool:
        ok = self.results.pop(0) if self.results else self.default
        if ok:
            self.published.append((topic, payload))
        return ok

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def payloads(messages: list[BufferedMessage]) -> list[str]:
    return [m.payload for m in messages]


def as_dict(**overrides: Any) -> dict[str, Any]:
    """A complete, valid set of decoded telemetry fields."""
    from telemetry_common.schema import EngineStatus

    fields: dict[str, Any] = {
        "speed": 50.0,
        "rpm": 2000.0,
        "coolant_temperature": 90.0,
        "battery_voltage": 14.1,
        "fuel_level": 60.0,
        "latitude": 49.0134,
        "longitude": 12.1016,
        "engine_status": EngineStatus.RUNNING,
        "diagnostic_code": None,
    }
    fields.update(overrides)
    return fields
