from __future__ import annotations

import pytest

from sim_helpers import make_model
from vehicle_simulator.model import VehicleModel


@pytest.fixture
def model() -> VehicleModel:
    return make_model()
