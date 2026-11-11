"""Processing logic, independent of how events arrive (Lambda or local MQTT bridge)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from telemetry_processor.dlq import DeadLetterQueue
from telemetry_processor.metrics import emit_metric
from telemetry_processor.repository import TelemetryRepository
from telemetry_processor.transform import to_latest_item, to_telemetry_item
from telemetry_processor.validation import MalformedMessageError, parse_event

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TelemetryProcessor:
    """Validate, transform and store one telemetry event.

    Error policy -- the key design decision of this component:

    * **Malformed** input is *permanent*: retrying cannot help. It is logged, counted,
      sent to the DLQ and the invocation succeeds (``Outcome.REJECTED``).
    * **Infrastructure** failures (throttling, outage, permissions) are *transient*: the
      exception propagates so the Lambda retry / on-failure destination handles them.
    """

    def __init__(
        self,
        repository: TelemetryRepository,
        dlq: DeadLetterQueue | None,
        *,
        ttl_days: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._dlq = dlq
        self._ttl_days = ttl_days
        self._clock = clock

    def process(self, event: object) -> Outcome:
        started = time.perf_counter()
        try:
            message = parse_event(event)
        except MalformedMessageError as exc:
            return self.reject(exc.reason, exc.details, event)

        now = self._clock()
        stored = self._repository.put_telemetry(
            to_telemetry_item(message, now=now, ttl_days=self._ttl_days)
        )
        # Always attempt the latest-state update: it is idempotent, and it repairs the case
        # where a previous invocation stored the telemetry but failed before this write.
        self._repository.put_latest(to_latest_item(message, now=now))

        outcome = Outcome.STORED if stored else Outcome.DUPLICATE
        emit_metric("MessagesStored" if stored else "DuplicateMessages")
        emit_metric("ProcessingLatencyMs", (time.perf_counter() - started) * 1000, "Milliseconds")
        # Ingest lag: sampling time at the gateway -> arrival here (includes buffering time).
        lag_ms = (now - message.timestamp).total_seconds() * 1000
        emit_metric("IngestLagMs", max(lag_ms, 0.0), "Milliseconds")
        logger.info(
            "telemetry_processed",
            extra={
                "outcome": outcome.value,
                "vehicle_id": message.vehicle_id,
                "message_id": str(message.message_id),
                "sequence": message.device.sequence,
            },
        )
        return outcome

    def reject(self, reason: str, details: list[str], original: object) -> Outcome:
        """Handle a message that can never be processed (see class docstring)."""
        logger.warning("message_rejected", extra={"reason": reason, "details": details})
        emit_metric("MalformedMessages")
        if self._dlq is not None:
            self._dlq.send(reason, details, original)
        return Outcome.REJECTED
