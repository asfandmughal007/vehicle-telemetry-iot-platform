"""Access to the vehicle CAN database (DBC).

The DBC file is the single source of truth for the CAN layout: frame IDs, bit
positions, scaling, value ranges and cycle times. Simulator (encode) and gateway
(decode) both load it from here, so they cannot disagree about a signal.

Requires the ``can`` extra (``pip install telemetry-common[can]``).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from importlib.resources import files
from typing import TYPE_CHECKING

import cantools

from telemetry_common.schema import EngineStatus

if TYPE_CHECKING:
    from cantools.database.can import Database

_DBC_PACKAGE = "telemetry_common"
_DBC_RESOURCE = "data/vehicle.dbc"


class FrameName(StrEnum):
    """Names of the CAN messages defined in the DBC."""

    ENGINE_DATA = "EngineData"
    VEHICLE_DYNAMICS = "VehicleDynamics"
    BATTERY_STATUS = "BatteryStatus"
    GNSS_POSITION = "GnssPosition"
    DIAGNOSTIC_TROUBLE_CODE = "DiagnosticTroubleCode"


#: Raw value of the ``EngineStatus`` signal (must match ``VAL_`` in the DBC).
ENGINE_STATUS_TO_RAW: dict[EngineStatus, int] = {
    EngineStatus.OFF: 0,
    EngineStatus.IDLE: 1,
    EngineStatus.RUNNING: 2,
    EngineStatus.FAULT: 3,
}
RAW_TO_ENGINE_STATUS: dict[int, EngineStatus] = {
    raw: status for status, raw in ENGINE_STATUS_TO_RAW.items()
}


@lru_cache(maxsize=1)
def load_vehicle_dbc() -> Database:
    """Load and cache the bundled vehicle DBC.

    The database is parsed once per process; callers must treat it as read-only.
    """
    dbc_text = files(_DBC_PACKAGE).joinpath(_DBC_RESOURCE).read_text(encoding="utf-8")
    database = cantools.database.can.Database()
    database.add_dbc_string(dbc_text)
    return database
