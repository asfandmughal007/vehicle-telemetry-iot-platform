from __future__ import annotations

import random

import pytest

from vehicle_simulator.driving_cycle import MAX_SPEED_CHANGE_DURATION_S, DrivingCycle, Phase
from vehicle_simulator.faults import FaultInjector, FaultType

DT = 0.5

# --- driving cycle -----------------------------------------------------------


def test_cycle_starts_parked_with_engine_off() -> None:
    cycle = DrivingCycle(random.Random(1))

    assert cycle.phase is Phase.PARKED
    assert not cycle.engine_on
    assert cycle.target_speed == 0.0


def test_cycle_starts_engine_then_accelerates() -> None:
    cycle = DrivingCycle(random.Random(1))
    phases = [cycle.phase]

    for _ in range(100):
        cycle.update(DT, current_speed=0.0)
        if cycle.phase is not phases[-1]:
            phases.append(cycle.phase)
        if cycle.phase is Phase.ACCELERATING:
            break

    assert phases == [Phase.PARKED, Phase.IDLE, Phase.ACCELERATING]
    assert cycle.engine_on
    assert 25.0 <= cycle.target_speed <= 137.0


def test_cycle_visits_every_phase_when_speed_tracks_target() -> None:
    cycle = DrivingCycle(random.Random(5))
    seen = {cycle.phase}

    for _ in range(40_000):
        cycle.update(DT, current_speed=cycle.target_speed)  # ideal vehicle
        seen.add(cycle.phase)

    assert seen == set(Phase)


def test_acceleration_phase_times_out_if_target_is_unreachable() -> None:
    cycle = DrivingCycle(random.Random(1))
    while cycle.phase is not Phase.ACCELERATING:
        cycle.update(DT, current_speed=0.0)

    elapsed = 0.0
    while cycle.phase is Phase.ACCELERATING:
        cycle.update(DT, current_speed=0.0)  # vehicle never moves
        elapsed += DT
        assert elapsed <= MAX_SPEED_CHANGE_DURATION_S + DT

    assert cycle.phase is Phase.CRUISING


def test_cycle_rejects_invalid_park_probability() -> None:
    with pytest.raises(ValueError, match="park_probability"):
        DrivingCycle(random.Random(), park_probability=1.5)


# --- fault injector ----------------------------------------------------------


def test_no_random_faults_when_rate_is_zero() -> None:
    injector = FaultInjector(random.Random(1), rate_per_hour=0.0)

    for _ in range(10_000):
        injector.update(1.0, engine_running=True)

    assert injector.active is None


def test_no_random_faults_while_engine_is_off() -> None:
    injector = FaultInjector(random.Random(1), rate_per_hour=3600.0)

    for _ in range(1_000):
        injector.update(1.0, engine_running=False)

    assert injector.active is None


def test_fault_rate_matches_poisson_expectation() -> None:
    injector = FaultInjector(random.Random(11), rate_per_hour=60.0)
    starts = 0
    previously_active = False

    for _ in range(100 * 3600):  # 100 engine hours at 1 s resolution
        injector.update(1.0, engine_running=True)
        if injector.active is not None and not previously_active:
            starts += 1
        previously_active = injector.active is not None

    # 60/h nominal, reduced because no new fault starts while one is active
    # (mean duration ~2 min) -> roughly 20/h. Wide bounds keep the test robust.
    assert 1000 < starts < 3500


def test_injected_fault_expires_after_duration() -> None:
    injector = FaultInjector(random.Random(1))
    injector.inject(FaultType.MISFIRE, duration_s=2.0)
    assert injector.is_active(FaultType.MISFIRE)
    assert not injector.is_active(FaultType.OVERHEAT)

    injector.update(1.0, engine_running=True)
    assert injector.active is FaultType.MISFIRE
    injector.update(1.0, engine_running=True)

    assert injector.active is None


def test_injector_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="rate_per_hour"):
        FaultInjector(random.Random(), rate_per_hour=-1.0)
    with pytest.raises(ValueError, match="duration_s"):
        FaultInjector(random.Random()).inject(FaultType.OVERHEAT, duration_s=0.0)
