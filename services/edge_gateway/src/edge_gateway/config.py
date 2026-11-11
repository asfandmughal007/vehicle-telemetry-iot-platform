"""Gateway configuration, read from environment variables (prefix ``GW_``).

Certificates are referenced by *path* and mounted at runtime -- key material is never
part of the configuration values or the container image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from telemetry_common.schema import VEHICLE_ID_PATTERN


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GW_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # --- identity
    vehicle_id: str = Field(default="VH-0001", pattern=VEHICLE_ID_PATTERN)
    gateway_id: str | None = Field(
        default=None, description="Defaults to gw-<vehicle_id>; used as MQTT client id."
    )
    firmware_version: str = "0.1.0"

    # --- CAN (must match the simulator / the real bus)
    can_interface: str = "udp_multicast"
    can_channel: str = "239.74.163.2"

    # --- sampling
    publish_hz: float = Field(default=1.0, gt=0, le=10, description="Cloud publish rate.")
    signal_max_age_s: float = Field(
        default=3.0, gt=0, description="A signal older than this is stale; no snapshot is sent."
    )

    # --- MQTT
    mqtt_host: str = "localhost"
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_keepalive_s: int = Field(default=30, ge=5)
    mqtt_tls: bool = False
    mqtt_ca_file: Path | None = None
    mqtt_cert_file: Path | None = None
    mqtt_key_file: Path | None = None
    mqtt_reconnect_min_s: float = Field(default=1.0, gt=0)
    mqtt_reconnect_max_s: float = Field(default=60.0, gt=0)
    mqtt_ack_timeout_s: float = Field(default=5.0, gt=0)

    # --- store-and-forward
    buffer_path: Path = Path("data/buffer/gateway.sqlite3")
    buffer_max_messages: int = Field(default=100_000, ge=1)
    forward_batch_size: int = Field(default=50, ge=1)

    # --- logging
    log_level: str = Field(default="INFO", pattern=r"^(?i)(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_json: bool = True

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.mqtt_reconnect_max_s < self.mqtt_reconnect_min_s:
            raise ValueError("mqtt_reconnect_max_s must be >= mqtt_reconnect_min_s")
        if self.mqtt_tls and not (self.mqtt_ca_file and self.mqtt_cert_file and self.mqtt_key_file):
            raise ValueError(
                "mqtt_tls requires GW_MQTT_CA_FILE, GW_MQTT_CERT_FILE and GW_MQTT_KEY_FILE"
            )
        return self

    @property
    def effective_gateway_id(self) -> str:
        return self.gateway_id or f"gw-{self.vehicle_id}"
