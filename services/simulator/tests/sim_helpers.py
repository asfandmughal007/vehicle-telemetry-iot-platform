"""Shared helpers for the simulator tests."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from vehicle_simulator.model import VehicleModel

FIXED_TIME = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
START_LATITUDE = 49.0134
START_LONGITUDE = 12.1016


def make_model(seed: int = 42, *, fault_rate_per_hour: float = 0.0) -> VehicleModel:
    return VehicleModel(
        "VH-TEST",
        start_latitude=START_LATITUDE,
        start_longitude=START_LONGITUDE,
        rng=random.Random(seed),
        fault_rate_per_hour=fault_rate_per_hour,
        clock=lambda: FIXED_TIME,
    )
