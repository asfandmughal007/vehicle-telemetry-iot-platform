from __future__ import annotations

import itertools
import json
import math

import pytest

from sim_helpers import FIXED_TIME, START_LATITUDE, START_LONGITUDE, make_model
from telemetry_common.schema import EngineStatus
from vehicle_simulator.driving_cycle import Phase
from vehicle_simulator.faults import FaultType
from vehicle_simulator.model import (
    AMBIENT_TEMPERATURE_C,
    DTC_LOW_VOLTAGE,
    DTC_MISFIRE,
    DTC_OVERHEAT,
    LIMP_MODE_SPEED_KMH,
    MAX_ACCELERATION_KMH_PER_S,
    VehicleModel,
    VehicleSnapshot,
)

DT = 0.1


def run(model: VehicleModel, seconds: float, dt: float = DT) -> list[VehicleSnapshot]:
    return [model.step(dt) for _ in range(round(seconds / dt))]


def drive_until(model: VehicleModel, phase: Phase, timeout_s: float = 600.0) -> None:
    for _ in range(round(timeout_s / DT)):
        model.step(DT)
        if model.cycle.phase is phase:
            return
    pytest.fail(f"model never reached phase {phase}")


def test_initial_state_is_parked_and_cold(model: VehicleModel) -> None:
    snapshot = model.snapshot()

    assert snapshot.vehicle_id == "VH-TEST"
    assert snapshot.timestamp == FIXED_TIME
    assert snapshot.engine_status is EngineStatus.OFF
    assert snapshot.speed == 0.0
    assert snapshot.rpm == 0.0
    assert snapshot.coolant_temperature == AMBIENT_TEMPERATURE_C
    assert snapshot.diagnostic_code is None
    assert (snapshot.latitude, snapshot.longitude) == (START_LATITUDE, START_LONGITUDE)


@pytest.mark.parametrize("dt", [0.0, -0.1, math.nan, math.inf])
def test_step_rejects_invalid_time_step(model: VehicleModel, dt: float) -> None:
    with pytest.raises(ValueError, match="dt must be"):
        model.step(dt)


def test_same_seed_gives_identical_drive() -> None:
    first = run(make_model(seed=7, fault_rate_per_hour=30.0), 300)
    second = run(make_model(seed=7, fault_rate_per_hour=30.0), 300)

    assert first == second


def test_different_seeds_give_different_drives() -> None:
    assert run(make_model(seed=1), 120) != run(make_model(seed=2), 120)


def test_signals_stay_within_contract_ranges_over_a_long_drive() -> None:
    snapshots = run(make_model(seed=3, fault_rate_per_hour=60.0), 3600, dt=0.5)

    for s in snapshots:
        assert 0.0 <= s.speed <= 300.0
        assert 0.0 <= s.rpm <= 8000.0
        assert -40.0 <= s.coolant_temperature <= 150.0
        assert 0.0 <= s.battery_voltage <= 30.0
        assert 0.0 <= s.fuel_level <= 100.0
        assert -90.0 <= s.latitude <= 90.0
        assert -180.0 <= s.longitude <= 180.0
    assert {s.engine_status for s in snapshots} >= {EngineStatus.IDLE, EngineStatus.RUNNING}
    assert max(s.speed for s in snapshots) > 40.0


def test_engine_off_means_zero_rpm_and_speed_decays() -> None:
    snapshots = run(make_model(seed=3), 1800, dt=0.5)
    parked = [s for s in snapshots if s.engine_status is EngineStatus.OFF]

    assert parked, "expected the vehicle to be parked at least once"
    assert all(s.rpm == 0.0 for s in parked)


def test_rpm_follows_speed_while_driving(model: VehicleModel) -> None:
    drive_until(model, Phase.CRUISING)
    snapshot = model.snapshot()

    assert snapshot.speed > 20.0
    assert 1000.0 < snapshot.rpm < 6500.0
    assert snapshot.engine_status is EngineStatus.RUNNING


def test_acceleration_is_physically_limited(model: VehicleModel) -> None:
    snapshots = run(model, 300)

    max_delta = max(b.speed - a.speed for a, b in itertools.pairwise(snapshots))
    # limit per step plus a small allowance for cruise noise
    assert max_delta <= MAX_ACCELERATION_KMH_PER_S * DT + 0.5


def test_coolant_warms_up_towards_operating_temperature(model: VehicleModel) -> None:
    snapshots = run(model, 600)

    assert snapshots[-1].coolant_temperature > 80.0
    assert max(s.coolant_temperature for s in snapshots) < 100.0


def test_battery_charges_when_engine_runs(model: VehicleModel) -> None:
    drive_until(model, Phase.CRUISING)
    run(model, 60)

    assert model.snapshot().battery_voltage == pytest.approx(14.1, abs=0.2)


def test_cranking_dip_does_not_raise_low_voltage_dtc(model: VehicleModel) -> None:
    snapshots = run(model, 60)

    assert min(s.battery_voltage for s in snapshots) < 11.8  # the dip happened
    assert all(s.diagnostic_code is None for s in snapshots)  # ...but was debounced


def test_fuel_is_consumed_and_position_changes_while_driving(model: VehicleModel) -> None:
    start = model.snapshot()

    end = run(model, 600)[-1]

    assert end.fuel_level < start.fuel_level
    assert (end.latitude, end.longitude) != (start.latitude, start.longitude)
    # sanity: cannot have moved further than 600 s at 250 km/h (~42 km ~ 0.6 deg)
    assert abs(end.latitude - start.latitude) < 0.6


def test_overheat_fault_sets_dtc_and_limp_mode(model: VehicleModel) -> None:
    drive_until(model, Phase.CRUISING)
    run(model, 300)  # warm engine
    model.faults.inject(FaultType.OVERHEAT, duration_s=400)

    snapshots = run(model, 400)

    assert snapshots[-1].diagnostic_code == DTC_OVERHEAT
    assert EngineStatus.FAULT in {s.engine_status for s in snapshots}
    limp = [s for s in snapshots[-200:] if s.engine_status is EngineStatus.FAULT]
    assert limp
    assert all(s.speed <= LIMP_MODE_SPEED_KMH + 1.0 for s in limp)


def test_dtc_clears_after_fault_is_gone(model: VehicleModel) -> None:
    drive_until(model, Phase.CRUISING)
    run(model, 300)
    model.faults.inject(FaultType.OVERHEAT, duration_s=200)
    assert DTC_OVERHEAT in {s.diagnostic_code for s in run(model, 200)}

    snapshots = run(model, 600)

    assert snapshots[-1].diagnostic_code is None
    assert snapshots[-1].engine_status is not EngineStatus.FAULT


def test_alternator_failure_sets_low_voltage_dtc(model: VehicleModel) -> None:
    drive_until(model, Phase.CRUISING)
    model.faults.inject(FaultType.ALTERNATOR_FAILURE, duration_s=120)

    # Short enough that the engine is still running from the cruise phase.
    snapshots = run(model, 15)

    assert snapshots[-1].battery_voltage < 11.8
    assert snapshots[-1].diagnostic_code == DTC_LOW_VOLTAGE


def test_misfire_sets_dtc_after_maturation_time(model: VehicleModel) -> None:
    drive_until(model, Phase.CRUISING)
    model.faults.inject(FaultType.MISFIRE, duration_s=60)

    early = run(model, 1)
    later = run(model, 5)

    assert early[-1].diagnostic_code is None
    assert later[-1].diagnostic_code == DTC_MISFIRE


def test_snapshot_dict_is_json_serialisable(model: VehicleModel) -> None:
    payload = json.loads(json.dumps(run(model, 30)[-1].to_dict()))

    assert set(payload) == {
        "vehicle_id",
        "timestamp",
        "speed",
        "rpm",
        "coolant_temperature",
        "battery_voltage",
        "fuel_level",
        "latitude",
        "longitude",
        "engine_status",
        "diagnostic_code",
    }
    assert payload["timestamp"] == FIXED_TIME.isoformat()
