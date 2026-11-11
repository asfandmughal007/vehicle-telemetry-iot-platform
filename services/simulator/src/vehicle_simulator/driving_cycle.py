"""Driving cycle: a small state machine that decides *what the driver wants*.

It only produces a target speed and an engine on/off state. How fast the vehicle
actually follows that target is the job of :class:`vehicle_simulator.model.VehicleModel`.

::

    PARKED --> IDLE --> ACCELERATING --> CRUISING --> DECELERATING --+
       ^         ^            ^              |             |         |
       |         |            +--------------+             |         |
       |         +---------------- (stopped) --------------+         |
       +-------------------------- (parked) -------------------------+
"""

from __future__ import annotations

import logging
import random
from enum import StrEnum

logger = logging.getLogger(__name__)

#: Typical German speed limits; urban limits are listed twice to weight them higher.
ROAD_SPEED_LIMITS_KMH: tuple[float, ...] = (30.0, 50.0, 50.0, 70.0, 100.0, 130.0)

ARRIVAL_TOLERANCE_KMH = 1.5
#: Safety net: leave a speed-change phase even if the target is never reached
#: (e.g. the engine is in limp mode and cannot reach the target speed).
MAX_SPEED_CHANGE_DURATION_S = 90.0
STOP_PROBABILITY = 0.35


class Phase(StrEnum):
    PARKED = "parked"
    IDLE = "idle"
    ACCELERATING = "accelerating"
    CRUISING = "cruising"
    DECELERATING = "decelerating"


class DrivingCycle:
    """Randomised but reproducible driver behaviour.

    Args:
        rng: Random source; pass a seeded instance for deterministic runs.
        park_probability: Chance that a full stop turns into parking (engine off).
    """

    def __init__(self, rng: random.Random, *, park_probability: float = 0.15) -> None:
        if not 0.0 <= park_probability <= 1.0:
            raise ValueError("park_probability must be within [0, 1]")
        self._rng = rng
        self._park_probability = park_probability
        self.phase = Phase.PARKED
        self.target_speed = 0.0
        self._remaining_s = rng.uniform(2.0, 6.0)
        self._phase_elapsed_s = 0.0

    @property
    def engine_on(self) -> bool:
        return self.phase is not Phase.PARKED

    def update(self, dt: float, current_speed: float) -> None:
        """Advance the state machine by ``dt`` seconds."""
        self._remaining_s -= dt
        self._phase_elapsed_s += dt
        timed_out = self._phase_elapsed_s >= MAX_SPEED_CHANGE_DURATION_S

        if self.phase is Phase.PARKED:
            if self._remaining_s <= 0:
                self._enter(Phase.IDLE, duration_s=self._rng.uniform(3.0, 10.0))
        elif self.phase is Phase.IDLE:
            if self._remaining_s <= 0:
                self._enter(Phase.ACCELERATING, target_speed=self._pick_cruise_speed())
        elif self.phase is Phase.ACCELERATING:
            if current_speed >= self.target_speed - ARRIVAL_TOLERANCE_KMH or timed_out:
                self._enter(Phase.CRUISING, duration_s=self._rng.uniform(20.0, 120.0))
        elif self.phase is Phase.CRUISING:
            if self._remaining_s <= 0:
                self._leave_cruise(current_speed)
        elif self.phase is Phase.DECELERATING and (
            current_speed <= self.target_speed + ARRIVAL_TOLERANCE_KMH or timed_out
        ):
            self._finish_deceleration()

    def _leave_cruise(self, current_speed: float) -> None:
        if self._rng.random() < STOP_PROBABILITY:
            self._enter(Phase.DECELERATING, target_speed=0.0)
            return
        new_target = self._pick_cruise_speed()
        phase = Phase.ACCELERATING if new_target > current_speed else Phase.DECELERATING
        self._enter(phase, target_speed=new_target)

    def _finish_deceleration(self) -> None:
        if self.target_speed > 0:
            self._enter(Phase.CRUISING, duration_s=self._rng.uniform(20.0, 120.0))
        elif self._rng.random() < self._park_probability:
            self._enter(Phase.PARKED, duration_s=self._rng.uniform(20.0, 90.0))
        else:  # traffic light / junction
            self._enter(Phase.IDLE, duration_s=self._rng.uniform(5.0, 30.0))

    def _pick_cruise_speed(self) -> float:
        limit = self._rng.choice(ROAD_SPEED_LIMITS_KMH)
        return limit * self._rng.uniform(0.85, 1.05)

    def _enter(
        self, phase: Phase, *, duration_s: float = 0.0, target_speed: float | None = None
    ) -> None:
        previous = self.phase
        self.phase = phase
        self._remaining_s = duration_s
        self._phase_elapsed_s = 0.0
        if target_speed is not None:
            self.target_speed = target_speed
        elif phase in (Phase.PARKED, Phase.IDLE):
            self.target_speed = 0.0
        logger.debug(
            "driving_phase_changed",
            extra={
                "from_phase": previous.value,
                "to_phase": phase.value,
                "target_speed": round(self.target_speed, 1),
            },
        )
