"""Latest value of every signal, with age tracking.

CAN frames arrive at 10-1000 ms cycles; the cloud wants one consistent snapshot per
second. The cache decouples both rates and refuses to build a snapshot from signals
that are missing or have not been refreshed recently (a dead ECU must not be
reported as "speed 0").
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

#: Fields that must be present and fresh for a telemetry snapshot.
REQUIRED_FIELDS: tuple[str, ...] = (
    "speed",
    "rpm",
    "coolant_temperature",
    "battery_voltage",
    "fuel_level",
    "latitude",
    "longitude",
    "engine_status",
    "diagnostic_code",
)


class IncompleteSnapshotError(Exception):
    """Raised when required signals are missing or stale."""

    def __init__(self, missing: list[str], stale: list[str]) -> None:
        self.missing = missing
        self.stale = stale
        super().__init__(f"missing={missing} stale={stale}")


class SignalCache:
    """Not thread-safe by design: only the CAN/main thread touches it."""

    def __init__(self, max_age_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        if max_age_s <= 0:
            raise ValueError("max_age_s must be > 0")
        self._max_age_s = max_age_s
        self._clock = clock
        self._values: dict[str, tuple[Any, float]] = {}

    def update(self, fields: Mapping[str, Any]) -> None:
        now = self._clock()
        for name, value in fields.items():
            self._values[name] = (value, now)

    def snapshot(self) -> dict[str, Any]:
        """Return all required fields, or raise :class:`IncompleteSnapshotError`."""
        now = self._clock()
        missing = [f for f in REQUIRED_FIELDS if f not in self._values]
        stale = [
            f
            for f in REQUIRED_FIELDS
            if f in self._values and now - self._values[f][1] > self._max_age_s
        ]
        if missing or stale:
            raise IncompleteSnapshotError(missing, stale)
        return {f: self._values[f][0] for f in REQUIRED_FIELDS}
