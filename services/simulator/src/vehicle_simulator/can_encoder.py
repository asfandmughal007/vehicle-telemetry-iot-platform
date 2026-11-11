"""Encode vehicle snapshots into CAN frames using the vehicle DBC."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

import can

from telemetry_common.can_db import ENGINE_STATUS_TO_RAW, FrameName, load_vehicle_dbc
from telemetry_common.dtc import encode_dtc
from vehicle_simulator.model import VehicleSnapshot

if TYPE_CHECKING:
    from cantools.database.can import Database

_DEFAULT_CYCLE_TIME_S = 0.1
#: Accept a tick that is this much early, so float jitter does not skip a cycle.
_SCHEDULE_TOLERANCE_S = 1e-3

SignalValues = dict[str, float | int]


class CanFrameEncoder:
    """Turns a :class:`VehicleSnapshot` into the CAN frames defined in the DBC.

    Encoding is strict: a value outside the signal range declared in the DBC raises
    instead of silently wrapping around, which would put a wrong value on the bus.
    """

    def __init__(self, database: Database | None = None) -> None:
        db = database if database is not None else load_vehicle_dbc()
        self._messages = {name: db.get_message_by_name(name.value) for name in FrameName}

    def cycle_times_s(self) -> dict[FrameName, float]:
        """Cycle time per frame in seconds, from the DBC attribute ``GenMsgCycleTime``."""
        return {
            name: (message.cycle_time / 1000.0 if message.cycle_time else _DEFAULT_CYCLE_TIME_S)
            for name, message in self._messages.items()
        }

    def encode(
        self, snapshot: VehicleSnapshot, frames: Iterable[FrameName] | None = None
    ) -> list[can.Message]:
        """Encode ``snapshot`` into CAN messages.

        Args:
            snapshot: Vehicle state to encode.
            frames: Subset of frames to produce; all frames if ``None``.
        """
        signals = self._signal_values(snapshot)
        encoded: list[can.Message] = []
        for name in frames if frames is not None else FrameName:
            message = self._messages[name]
            encoded.append(
                can.Message(
                    arbitration_id=message.frame_id,
                    data=message.encode(signals[name], strict=True),
                    is_extended_id=message.is_extended_frame,
                )
            )
        return encoded

    @staticmethod
    def _signal_values(snapshot: VehicleSnapshot) -> Mapping[FrameName, SignalValues]:
        dtc_active, dtc_category, dtc_code = 0, 0, 0
        if snapshot.diagnostic_code is not None:
            dtc_category, dtc_code = encode_dtc(snapshot.diagnostic_code)
            dtc_active = 1

        return {
            FrameName.ENGINE_DATA: {
                "EngineSpeed": snapshot.rpm,
                "CoolantTemperature": snapshot.coolant_temperature,
                "EngineStatus": ENGINE_STATUS_TO_RAW[snapshot.engine_status],
            },
            FrameName.VEHICLE_DYNAMICS: {
                "VehicleSpeed": snapshot.speed,
                "FuelLevel": snapshot.fuel_level,
            },
            FrameName.BATTERY_STATUS: {"BatteryVoltage": snapshot.battery_voltage},
            FrameName.GNSS_POSITION: {
                "Latitude": snapshot.latitude,
                "Longitude": snapshot.longitude,
            },
            FrameName.DIAGNOSTIC_TROUBLE_CODE: {
                "DtcActive": dtc_active,
                "DtcCategory": dtc_category,
                "DtcCode": dtc_code,
            },
        }


class CyclicFrameScheduler:
    """Decides which frames are due, given a cycle time per frame.

    Real ECUs do not send every message at the same rate: fast-changing signals go
    out every 10-100 ms, slow ones (position, voltage) once per second.
    """

    def __init__(self, cycle_times_s: Mapping[FrameName, float]) -> None:
        for name, period in cycle_times_s.items():
            if period <= 0:
                raise ValueError(f"cycle time of {name} must be > 0")
        self._cycle_times_s = dict(cycle_times_s)
        self._next_due: dict[FrameName, float] = {}

    def due(self, now: float) -> list[FrameName]:
        """Return the frames to send at time ``now`` (monotonic seconds)."""
        due: list[FrameName] = []
        for name, period in self._cycle_times_s.items():
            next_due = self._next_due.get(name, now)
            if now + _SCHEDULE_TOLERANCE_S < next_due:
                continue
            due.append(name)
            next_due += period
            # After a long stall, do not burst to catch up -- restart the cycle.
            self._next_due[name] = next_due if next_due > now else now + period
        return due
