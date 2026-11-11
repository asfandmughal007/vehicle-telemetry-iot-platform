"""Simulator configuration, read from environment variables (prefix ``SIM_``)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from telemetry_common.schema import VEHICLE_ID_PATTERN


class OutputMode(StrEnum):
    """Where the simulator sends its data."""

    CAN = "can"
    STDOUT = "stdout"


class SimulatorSettings(BaseSettings):
    """All tunables of the simulator.

    Every field maps to ``SIM_<FIELD_NAME>``; see ``.env.example`` in the repository root.
    Invalid values fail fast at start-up with a readable validation error.
    """

    model_config = SettingsConfigDict(
        env_prefix="SIM_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    vehicle_id: str = Field(default="VH-0001", pattern=VEHICLE_ID_PATTERN)
    output: OutputMode = OutputMode.CAN

    # python-can bus selection. ``udp_multicast`` works between Docker containers,
    # ``socketcan`` talks to a real or virtual (vcan) Linux CAN interface.
    can_interface: str = "udp_multicast"
    can_channel: str = "239.74.163.2"

    tick_hz: float = Field(default=10.0, gt=0, le=100, description="Model update rate.")
    duration_s: float | None = Field(default=None, gt=0, description="Stop after N seconds.")
    seed: int | None = Field(default=None, description="Fixed seed for a reproducible drive.")
    fault_rate_per_hour: float = Field(default=6.0, ge=0, le=3600)

    # Default start position: Regensburg, Germany.
    start_latitude: float = Field(default=49.0134, ge=-90, le=90)
    start_longitude: float = Field(default=12.1016, ge=-180, le=180)

    log_level: str = Field(default="INFO", pattern=r"^(?i)(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_json: bool = True
