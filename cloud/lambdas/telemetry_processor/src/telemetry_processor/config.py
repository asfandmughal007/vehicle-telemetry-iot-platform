"""Processor configuration from environment variables (Lambda environment or local container)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_TTL_DAYS = 30


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ProcessorConfig:
    telemetry_table: str
    vehicles_table: str
    dlq_url: str | None = None
    ttl_days: int = DEFAULT_TTL_DAYS
    #: Set only for local development (DynamoDB Local); ``None`` means real AWS.
    endpoint_url: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProcessorConfig:
        source = os.environ if env is None else env

        def required(name: str) -> str:
            value = source.get(name, "").strip()
            if not value:
                raise ConfigError(f"environment variable {name} is required")
            return value

        try:
            ttl_days = int(source.get("TTL_DAYS") or DEFAULT_TTL_DAYS)
        except ValueError as exc:
            raise ConfigError("TTL_DAYS must be an integer") from exc
        if ttl_days < 1:
            raise ConfigError("TTL_DAYS must be >= 1")

        return cls(
            telemetry_table=required("TELEMETRY_TABLE"),
            vehicles_table=required("VEHICLES_TABLE"),
            dlq_url=source.get("DLQ_URL") or None,
            ttl_days=ttl_days,
            endpoint_url=source.get("DYNAMODB_ENDPOINT_URL") or None,
        )
