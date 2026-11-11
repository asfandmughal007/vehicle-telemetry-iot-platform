"""Physics-inspired vehicle model.

Not a validated vehicle dynamics simulation -- the goal is telemetry that *behaves*
plausibly: signals are correlated (speed <-> RPM <-> fuel), have inertia (coolant
warm-up, acceleration limits) and react to faults.

The model is advanced with a fixed time step and has no dependency on wall-clock
time, so a seeded run is fully reproducible.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from telemetry_common.schema import EngineStatus
from vehicle_simulator.driving_cycle import DrivingCycle, Phase
from vehicle_simulator.faults import FaultInjector, FaultType

logger = logging.getLogger(__name__)

# --- Drivetrain --------------------------------------------------------------
GEAR_RATIOS: tuple[float, ...] = (3.60, 2.10, 1.40, 1.00, 0.80, 0.65)
FINAL_DRIVE_RATIO = 3.9
WHEEL_RADIUS_M = 0.32
IDLE_RPM = 800.0
MAX_RPM = 6500.0
#: Shift up as long as the next gear keeps the engine above this speed.
UPSHIFT_FLOOR_RPM = 1300.0
MAX_ACCELERATION_KMH_PER_S = 9.0  # ~2.5 m/s^2
MAX_DECELERATION_KMH_PER_S = 12.0  # ~3.3 m/s^2
MAX_SPEED_KMH = 250.0
LIMP_MODE_SPEED_KMH = 40.0

# --- Thermal -----------------------------------------------------------------
AMBIENT_TEMPERATURE_C = 18.0
THERMOSTAT_TEMPERATURE_C = 90.0
LOAD_TEMPERATURE_RISE_C = 6.0
OVERHEAT_TARGET_C = 125.0
WARMUP_TAU_S = 90.0
REGULATION_TAU_S = 60.0
COOLDOWN_TAU_S = 900.0

# --- Electrical --------------------------------------------------------------
RESTING_VOLTAGE_V = 12.5
CHARGING_VOLTAGE_V = 14.1
DISCHARGING_VOLTAGE_V = 11.3  # engine running without alternator
CRANKING_VOLTAGE_V = 10.6
#: Short enough that the cranking dip recovers before a DTC can mature.
VOLTAGE_TAU_S = 2.0

# --- Fuel --------------------------------------------------------------------
TANK_CAPACITY_L = 55.0
IDLE_CONSUMPTION_L_PER_H = 0.8
CONSUMPTION_L_PER_H_PER_1000_RPM = 4.0
REFUEL_THRESHOLD_PCT = 10.0
REFUEL_LEVEL_PCT = 95.0

# --- Position ----------------------------------------------------------------
METERS_PER_DEGREE_LATITUDE = 111_320.0
HEADING_NOISE_DEG_PER_SQRT_S = 3.0

# --- Diagnostics -------------------------------------------------------------
DTC_OVERHEAT = "P0217"  # Engine coolant over-temperature condition
DTC_LOW_VOLTAGE = "P0562"  # System voltage low
DTC_MISFIRE = "P0300"  # Random/multiple cylinder misfire detected
OVERHEAT_DTC_THRESHOLD_C = 110.0
LIMP_MODE_THRESHOLD_C = 115.0
LOW_VOLTAGE_THRESHOLD_V = 11.8
#: A condition must be present this long before the DTC is set (debounce), so that
#: short events such as the voltage dip while cranking do not raise a code.
DTC_MATURATION_S = 3.0


@dataclass(frozen=True, slots=True)
class VehicleSnapshot:
    """All telemetry signals of the vehicle at one point in time."""

    vehicle_id: str
    timestamp: datetime
    speed: float  # km/h
    rpm: float  # 1/min
    coolant_temperature: float  # degC
    battery_voltage: float  # V
    fuel_level: float  # %
    latitude: float  # deg, WGS84
    longitude: float  # deg, WGS84
    engine_status: EngineStatus
    diagnostic_code: str | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation with sensible rounding."""
        return {
            "vehicle_id": self.vehicle_id,
            "timestamp": self.timestamp.isoformat(),
            "speed": round(self.speed, 2),
            "rpm": round(self.rpm, 1),
            "coolant_temperature": round(self.coolant_temperature, 1),
            "battery_voltage": round(self.battery_voltage, 3),
            "fuel_level": round(self.fuel_level, 1),
            "latitude": round(self.latitude, 7),
            "longitude": round(self.longitude, 7),
            "engine_status": self.engine_status.value,
            "diagnostic_code": self.diagnostic_code,
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _first_order(current: float, target: float, dt: float, tau: float) -> float:
    """First-order lag (PT1): move ``current`` towards ``target`` with time constant ``tau``."""
    return current + (target - current) * (1.0 - math.exp(-dt / tau))


class VehicleModel:
    """Simulated vehicle.

    Args:
        vehicle_id: Identifier attached to every snapshot.
        start_latitude: Initial WGS84 latitude in degrees.
        start_longitude: Initial WGS84 longitude in degrees.
        rng: Random source; pass ``random.Random(seed)`` for a reproducible drive.
        fault_rate_per_hour: Expected random faults per engine hour (0 = none).
        clock: Returns the timestamp for snapshots; injectable for tests.
    """

    def __init__(
        self,
        vehicle_id: str,
        *,
        start_latitude: float,
        start_longitude: float,
        rng: random.Random | None = None,
        fault_rate_per_hour: float = 0.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.vehicle_id = vehicle_id
        self._rng = rng if rng is not None else random.Random()
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self.cycle = DrivingCycle(self._rng)
        self.faults = FaultInjector(self._rng, rate_per_hour=fault_rate_per_hour)

        self._speed = 0.0
        self._rpm = 0.0
        self._coolant = AMBIENT_TEMPERATURE_C
        self._battery = RESTING_VOLTAGE_V
        self._fuel_l = TANK_CAPACITY_L * self._rng.uniform(0.4, 0.95)
        self._latitude = start_latitude
        self._longitude = start_longitude
        self._heading_deg = self._rng.uniform(0.0, 360.0)

        self._dtc: str | None = None
        self._condition_age_s: dict[str, float] = {}

    # ------------------------------------------------------------------ public

    def step(self, dt: float) -> VehicleSnapshot:
        """Advance the simulation by ``dt`` seconds and return the new state."""
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError(f"dt must be a positive, finite number of seconds, got {dt!r}")

        engine_was_on = self.cycle.engine_on
        self.cycle.update(dt, self._speed)
        self.faults.update(dt, engine_running=self.cycle.engine_on)

        if self.cycle.engine_on and not engine_was_on:
            self._battery = CRANKING_VOLTAGE_V  # starter motor load

        self._update_speed(dt)
        self._update_rpm()
        self._update_coolant(dt)
        self._update_battery(dt)
        self._update_fuel(dt)
        self._update_position(dt)
        self._update_diagnostics(dt)
        return self.snapshot()

    def snapshot(self) -> VehicleSnapshot:
        """Return the current state without advancing the simulation."""
        return VehicleSnapshot(
            vehicle_id=self.vehicle_id,
            timestamp=self._clock(),
            speed=self._speed,
            rpm=self._rpm,
            coolant_temperature=self._coolant,
            battery_voltage=self._battery,
            fuel_level=_clamp(self._fuel_l / TANK_CAPACITY_L * 100.0, 0.0, 100.0),
            latitude=self._latitude,
            longitude=self._longitude,
            engine_status=self.engine_status,
            diagnostic_code=self._dtc,
        )

    @property
    def engine_status(self) -> EngineStatus:
        if not self.cycle.engine_on:
            return EngineStatus.OFF
        if self._in_limp_mode:
            return EngineStatus.FAULT
        if self._speed < 1.0:
            return EngineStatus.IDLE
        return EngineStatus.RUNNING

    # ----------------------------------------------------------------- physics

    @property
    def _in_limp_mode(self) -> bool:
        return self._coolant >= LIMP_MODE_THRESHOLD_C

    @property
    def _engine_load(self) -> float:
        """Rough engine load in [0, 1]."""
        if not self.cycle.engine_on:
            return 0.0
        if self.cycle.phase is Phase.ACCELERATING:
            return 0.8
        return _clamp(0.1 + self._speed / 300.0, 0.0, 1.0)

    def _update_speed(self, dt: float) -> None:
        target = self.cycle.target_speed if self.cycle.engine_on else 0.0
        if self._in_limp_mode:
            target = min(target, LIMP_MODE_SPEED_KMH)

        error = target - self._speed
        limit = (MAX_ACCELERATION_KMH_PER_S if error > 0 else MAX_DECELERATION_KMH_PER_S) * dt
        self._speed += _clamp(error, -limit, limit)

        if self.cycle.phase is Phase.CRUISING and self._speed > 5.0:
            self._speed += self._rng.gauss(0.0, 0.4) * math.sqrt(dt)  # driver is not a controller
        self._speed = _clamp(self._speed, 0.0, MAX_SPEED_KMH)

    def _update_rpm(self) -> None:
        if not self.cycle.engine_on:
            self._rpm = 0.0
            return

        wheel_rpm = (self._speed / 3.6) / (2.0 * math.pi * WHEEL_RADIUS_M) * 60.0
        rpm = IDLE_RPM
        for ratio in GEAR_RATIOS:  # highest gear that keeps the engine above the floor
            candidate = wheel_rpm * ratio * FINAL_DRIVE_RATIO
            if candidate < UPSHIFT_FLOOR_RPM:
                break
            rpm = candidate
        if rpm == IDLE_RPM and wheel_rpm > 0:
            rpm = max(IDLE_RPM, wheel_rpm * GEAR_RATIOS[0] * FINAL_DRIVE_RATIO)

        if self.cycle.phase is Phase.ACCELERATING:
            rpm *= 1.15  # driver holds gears longer under load
        noise_std = 150.0 if self.faults.is_active(FaultType.MISFIRE) else 12.0
        self._rpm = _clamp(rpm + self._rng.gauss(0.0, noise_std), 0.0, MAX_RPM)

    def _update_coolant(self, dt: float) -> None:
        if not self.cycle.engine_on:
            self._coolant = _first_order(self._coolant, AMBIENT_TEMPERATURE_C, dt, COOLDOWN_TAU_S)
            return
        if self.faults.is_active(FaultType.OVERHEAT):
            target = OVERHEAT_TARGET_C
        else:
            target = THERMOSTAT_TEMPERATURE_C + LOAD_TEMPERATURE_RISE_C * self._engine_load
        tau = WARMUP_TAU_S if target > self._coolant else REGULATION_TAU_S
        self._coolant = _first_order(self._coolant, target, dt, tau)

    def _update_battery(self, dt: float) -> None:
        if not self.cycle.engine_on:
            target = RESTING_VOLTAGE_V
        elif self.faults.is_active(FaultType.ALTERNATOR_FAILURE):
            target = DISCHARGING_VOLTAGE_V
        else:
            target = CHARGING_VOLTAGE_V
        self._battery = _first_order(self._battery, target, dt, VOLTAGE_TAU_S)
        self._battery = _clamp(self._battery + self._rng.gauss(0.0, 0.004), 0.0, 30.0)

    def _update_fuel(self, dt: float) -> None:
        if self.cycle.engine_on:
            litres_per_hour = (
                IDLE_CONSUMPTION_L_PER_H
                + max(0.0, self._rpm - IDLE_RPM) / 1000.0 * CONSUMPTION_L_PER_H_PER_1000_RPM
            )
            self._fuel_l = max(0.0, self._fuel_l - litres_per_hour * dt / 3600.0)
        elif self._fuel_l / TANK_CAPACITY_L * 100.0 < REFUEL_THRESHOLD_PCT:
            self._fuel_l = TANK_CAPACITY_L * REFUEL_LEVEL_PCT / 100.0
            logger.info("vehicle_refuelled", extra={"vehicle_id": self.vehicle_id})

    def _update_position(self, dt: float) -> None:
        distance_m = self._speed / 3.6 * dt
        if distance_m <= 0:
            return
        self._heading_deg = (
            self._heading_deg + self._rng.gauss(0.0, HEADING_NOISE_DEG_PER_SQRT_S) * math.sqrt(dt)
        ) % 360.0
        heading = math.radians(self._heading_deg)
        metres_per_degree_longitude = METERS_PER_DEGREE_LATITUDE * max(
            math.cos(math.radians(self._latitude)), 1e-6
        )
        self._latitude = _clamp(
            self._latitude + distance_m * math.cos(heading) / METERS_PER_DEGREE_LATITUDE,
            -89.9,
            89.9,
        )
        longitude = self._longitude + distance_m * math.sin(heading) / metres_per_degree_longitude
        self._longitude = (longitude + 180.0) % 360.0 - 180.0  # wrap at the antimeridian

    # ------------------------------------------------------------- diagnostics

    def _update_diagnostics(self, dt: float) -> None:
        """Diagnostic monitor: derive the active DTC from signal conditions."""
        engine_on = self.cycle.engine_on
        # Ordered by priority: only the most severe active code is reported.
        conditions: tuple[tuple[str, bool], ...] = (
            (DTC_OVERHEAT, self._coolant >= OVERHEAT_DTC_THRESHOLD_C),
            (DTC_LOW_VOLTAGE, engine_on and self._battery < LOW_VOLTAGE_THRESHOLD_V),
            (DTC_MISFIRE, engine_on and self.faults.is_active(FaultType.MISFIRE)),
        )

        active: str | None = None
        for code, present in conditions:
            age = self._condition_age_s.get(code, 0.0) + dt if present else 0.0
            self._condition_age_s[code] = age
            if active is None and age >= DTC_MATURATION_S:
                active = code

        if active != self._dtc:
            logger.info(
                "dtc_changed",
                extra={"vehicle_id": self.vehicle_id, "previous": self._dtc, "current": active},
            )
            self._dtc = active
