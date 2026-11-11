"""Fault injection.

Faults change the *physics* of the vehicle model (e.g. the cooling system stops
working). They do not set diagnostic codes directly -- the model's diagnostic
monitor derives DTCs from the resulting signal values, like a real ECU does.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class FaultType(StrEnum):
    OVERHEAT = "overheat"  # cooling failure -> coolant temperature rises
    ALTERNATOR_FAILURE = "alternator_failure"  # no charging -> board voltage drops
    MISFIRE = "misfire"  # combustion misfire -> unstable engine speed


_DURATION_RANGE_S: dict[FaultType, tuple[float, float]] = {
    FaultType.OVERHEAT: (120.0, 240.0),
    FaultType.ALTERNATOR_FAILURE: (60.0, 180.0),
    FaultType.MISFIRE: (20.0, 90.0),
}


@dataclass(slots=True)
class _ActiveFault:
    fault: FaultType
    remaining_s: float


class FaultInjector:
    """Starts random faults as a Poisson process while the engine is running.

    Args:
        rng: Random source; pass a seeded instance for deterministic runs.
        rate_per_hour: Expected number of faults per engine hour. ``0`` disables
            random faults (faults can still be injected explicitly).
    """

    def __init__(self, rng: random.Random, *, rate_per_hour: float = 0.0) -> None:
        if rate_per_hour < 0:
            raise ValueError("rate_per_hour must be >= 0")
        self._rng = rng
        self._rate_per_s = rate_per_hour / 3600.0
        self._active: _ActiveFault | None = None

    @property
    def active(self) -> FaultType | None:
        return self._active.fault if self._active else None

    def is_active(self, fault: FaultType) -> bool:
        return self.active is fault

    def inject(self, fault: FaultType, duration_s: float) -> None:
        """Start ``fault`` now, replacing any active fault."""
        if duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        self._active = _ActiveFault(fault, duration_s)
        logger.info(
            "fault_injected", extra={"fault": fault.value, "duration_s": round(duration_s, 1)}
        )

    def update(self, dt: float, *, engine_running: bool) -> None:
        """Advance by ``dt`` seconds: expire the active fault or maybe start a new one."""
        if self._active is not None:
            self._active.remaining_s -= dt
            if self._active.remaining_s <= 0:
                logger.info("fault_cleared", extra={"fault": self._active.fault.value})
                self._active = None
            return

        if not engine_running or self._rate_per_s == 0:
            return
        # Probability of at least one event of a Poisson process within dt.
        if self._rng.random() < 1.0 - math.exp(-self._rate_per_s * dt):
            fault = self._rng.choice(list(FaultType))
            self.inject(fault, self._rng.uniform(*_DURATION_RANGE_S[fault]))
