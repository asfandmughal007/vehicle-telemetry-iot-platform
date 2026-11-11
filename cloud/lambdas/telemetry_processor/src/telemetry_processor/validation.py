"""Validation of incoming telemetry events.

The IoT rule ``SELECT *, topic(2) AS topic_vehicle_id FROM 'vehicles/+/telemetry'`` hands
the Lambda the device payload plus the vehicle ID taken from the *topic*. IoT policies
only let a device publish to its own topic, so the topic is trustworthy and the payload
is not -- a mismatch means a misconfigured or spoofing device and the message is rejected.
"""

from __future__ import annotations

from pydantic import ValidationError

from telemetry_common.schema import TelemetryMessage

TOPIC_VEHICLE_ID_KEY = "topic_vehicle_id"


class MalformedMessageError(Exception):
    """The event is not a valid telemetry message. It is dead-lettered, never retried."""

    def __init__(self, reason: str, details: list[str] | None = None) -> None:
        self.reason = reason
        self.details = details or []
        super().__init__(f"{reason}: {self.details}")


def parse_event(event: object) -> TelemetryMessage:
    """Validate a Lambda event and return the typed message.

    Raises:
        MalformedMessageError: for anything that is not a valid, consistent message.
    """
    if not isinstance(event, dict):
        raise MalformedMessageError("not_a_json_object", [type(event).__name__])

    payload = dict(event)
    topic_vehicle_id = payload.pop(TOPIC_VEHICLE_ID_KEY, None)
    if not isinstance(topic_vehicle_id, str):
        # Without the topic ID the spoofing check cannot be done: fail closed.
        raise MalformedMessageError("missing_topic_vehicle_id")

    try:
        message = TelemetryMessage.model_validate(payload)
    except ValidationError as exc:
        details = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise MalformedMessageError("schema_validation_failed", details) from exc

    if message.vehicle_id != topic_vehicle_id:
        raise MalformedMessageError(
            "vehicle_id_mismatch", [f"topic={topic_vehicle_id} payload={message.vehicle_id}"]
        )
    return message
