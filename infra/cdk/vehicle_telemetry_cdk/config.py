"""Deployment configuration. Everything environment-specific enters here (CDK context)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_VEHICLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class StackConfig:
    """Settings of one deployment.

    Attributes:
        environment: ``dev`` (resources are destroyed with the stack) or ``prod`` (data
            tables and the dashboard bucket are retained, deletion protection is on).
        vehicle_ids: Vehicles to register as IoT things. Each gets thing ``gw-<id>``.
        alarm_email: Receives alarm notifications (subscription must be confirmed).
        telemetry_ttl_days: Retention of raw telemetry (DynamoDB TTL).
        alarm_on_missing_telemetry: Alarm if no telemetry is stored for 15 minutes. Disable
            for fleets that are idle for long periods (it would be permanently in alarm).
        api_throttle_rate / api_throttle_burst: API Gateway default stage limits.
        api_cors_origins: Extra browser origins allowed to call the API (the CloudFront
            dashboard origin is added automatically).
    """

    environment: str = "dev"
    vehicle_ids: tuple[str, ...] = ("VH-0001",)
    alarm_email: str | None = None
    telemetry_ttl_days: int = 30
    alarm_on_missing_telemetry: bool = True
    api_throttle_rate: int = 20
    api_throttle_burst: int = 40
    api_cors_origins: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.environment not in {"dev", "prod"}:
            raise ValueError("environment must be 'dev' or 'prod'")
        if not self.vehicle_ids:
            raise ValueError("at least one vehicle id is required")
        for vehicle_id in self.vehicle_ids:
            if not _VEHICLE_ID_RE.fullmatch(vehicle_id):
                raise ValueError(f"invalid vehicle id: {vehicle_id!r}")
        if len(set(self.vehicle_ids)) != len(self.vehicle_ids):
            raise ValueError("vehicle ids must be unique")
        if self.alarm_email is not None and not _EMAIL_RE.fullmatch(self.alarm_email):
            raise ValueError("alarm_email is not a valid e-mail address")
        if self.telemetry_ttl_days < 1:
            raise ValueError("telemetry_ttl_days must be >= 1")
        if self.api_throttle_rate < 1 or self.api_throttle_burst < self.api_throttle_rate:
            raise ValueError("require 1 <= api_throttle_rate <= api_throttle_burst")

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @classmethod
    def from_context(cls, context: dict[str, object]) -> StackConfig:
        """Build from ``cdk deploy -c key=value`` context values (strings)."""

        def text(key: str) -> str | None:
            value = context.get(key)
            return str(value) if value not in (None, "") else None

        def csv(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = text(key)
            return tuple(p.strip() for p in raw.split(",") if p.strip()) if raw else default

        defaults = cls()
        return cls(
            environment=text("environment") or defaults.environment,
            vehicle_ids=csv("vehicle_ids", defaults.vehicle_ids),
            alarm_email=text("alarm_email"),
            telemetry_ttl_days=int(text("telemetry_ttl_days") or defaults.telemetry_ttl_days),
            alarm_on_missing_telemetry=(text("alarm_on_missing_telemetry") or "true").lower()
            != "false",
            api_cors_origins=csv("api_cors_origins", ()),
        )
