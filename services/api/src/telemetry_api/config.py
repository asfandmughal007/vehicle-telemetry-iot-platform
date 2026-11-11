"""API configuration from environment variables (prefix ``API_``)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="API_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    telemetry_table: str = "telemetry"
    vehicles_table: str = "vehicles"
    #: Only for local development (DynamoDB Local); leave unset in AWS.
    dynamodb_endpoint_url: str | None = None

    #: A vehicle counts as "online" if its last telemetry is younger than this.
    online_threshold_s: int = Field(default=120, ge=1)
    default_page_size: int = Field(default=100, ge=1, le=1000)
    max_page_size: int = Field(default=1000, ge=1, le=10_000)

    #: Comma-separated origins allowed to call the API from a browser (the dashboard).
    cors_origins: str = ""

    log_level: str = Field(default="INFO", pattern=r"^(?i)(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_json: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
