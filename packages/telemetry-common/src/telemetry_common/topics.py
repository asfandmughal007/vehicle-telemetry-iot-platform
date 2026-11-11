"""MQTT topic hierarchy.

::

    vehicles/{vehicle_id}/telemetry     device -> cloud, periodic snapshot
    vehicles/{vehicle_id}/status        device -> cloud, retained online/offline (+ LWT)
    vehicles/{vehicle_id}/diagnostics   device -> cloud, DTC set / cleared events
    vehicles/{vehicle_id}/commands      cloud -> device, back-channel

Topics are built and parsed only through this module so that publisher, IoT policy,
IoT rule and subscriber can never drift apart.
"""

from __future__ import annotations

import re
from enum import StrEnum

from telemetry_common.schema import VEHICLE_ID_PATTERN

TOPIC_ROOT = "vehicles"

_VEHICLE_ID_RE = re.compile(VEHICLE_ID_PATTERN)


class Channel(StrEnum):
    """Third topic level: what kind of message is transported."""

    TELEMETRY = "telemetry"
    STATUS = "status"
    DIAGNOSTICS = "diagnostics"
    COMMANDS = "commands"


class InvalidTopicError(ValueError):
    """Raised when a topic does not follow the platform hierarchy."""


def build_topic(vehicle_id: str, channel: Channel) -> str:
    """Return the concrete topic for one vehicle and channel.

    Raises:
        InvalidTopicError: if ``vehicle_id`` contains characters that are not allowed
            (this also rules out the MQTT wildcards ``+`` and ``#``).
    """
    if not _VEHICLE_ID_RE.fullmatch(vehicle_id):
        raise InvalidTopicError(f"invalid vehicle_id for topic: {vehicle_id!r}")
    return f"{TOPIC_ROOT}/{vehicle_id}/{channel.value}"


def subscription_filter(channel: Channel) -> str:
    """Return the wildcard filter matching ``channel`` for all vehicles."""
    return f"{TOPIC_ROOT}/+/{channel.value}"


def parse_topic(topic: str) -> tuple[str, Channel]:
    """Split a concrete topic into ``(vehicle_id, channel)``.

    Raises:
        InvalidTopicError: if the topic is not ``vehicles/{vehicle_id}/{channel}``.
    """
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != TOPIC_ROOT:
        raise InvalidTopicError(f"topic does not match '{TOPIC_ROOT}/<id>/<channel>': {topic!r}")
    _, vehicle_id, channel_name = parts
    if not _VEHICLE_ID_RE.fullmatch(vehicle_id):
        raise InvalidTopicError(f"invalid vehicle_id in topic: {topic!r}")
    try:
        channel = Channel(channel_name)
    except ValueError as exc:
        raise InvalidTopicError(f"unknown channel in topic: {topic!r}") from exc
    return vehicle_id, channel
