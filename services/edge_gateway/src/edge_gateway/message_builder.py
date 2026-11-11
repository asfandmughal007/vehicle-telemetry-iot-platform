"""Turn decoded signals into validated, enriched messages (timestamp, id, device metadata)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from telemetry_common.schema import (
    ConnectionStatus,
    DeviceMetadata,
    DiagnosticEvent,
    DiagnosticEventType,
    StatusMessage,
    TelemetryMessage,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MessageBuilder:
    """Creates schema-validated messages.

    Validation is the gate to the cloud: ``build_*`` raises
    :class:`pydantic.ValidationError` for implausible values, and the caller drops the
    message instead of publishing garbage.
    """

    def __init__(
        self,
        *,
        vehicle_id: str,
        gateway_id: str,
        firmware_version: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._vehicle_id = vehicle_id
        self._gateway_id = gateway_id
        self._firmware_version = firmware_version
        self._clock = clock
        self._sequence = 0

    def _device(self, sequence: int) -> DeviceMetadata:
        return DeviceMetadata(
            gateway_id=self._gateway_id,
            firmware_version=self._firmware_version,
            sequence=sequence,
        )

    def build_telemetry(self, fields: Mapping[str, Any]) -> TelemetryMessage:
        message = TelemetryMessage(
            message_id=uuid4(),
            vehicle_id=self._vehicle_id,
            timestamp=self._clock(),
            device=self._device(self._sequence),
            **fields,
        )
        # Only successfully built messages consume a sequence number, so a gap seen
        # in the cloud always means a message was lost after this point.
        self._sequence += 1
        return message

    def build_diagnostic_event(
        self, event: DiagnosticEventType, diagnostic_code: str
    ) -> DiagnosticEvent:
        return DiagnosticEvent(
            message_id=uuid4(),
            vehicle_id=self._vehicle_id,
            timestamp=self._clock(),
            event=event,
            diagnostic_code=diagnostic_code,
            device=self._device(self._sequence),
        )

    def build_status(self, status: ConnectionStatus) -> StatusMessage:
        return StatusMessage(
            vehicle_id=self._vehicle_id,
            status=status,
            timestamp=self._clock(),
            device=self._device(self._sequence),
        )
