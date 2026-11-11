from __future__ import annotations

import pytest

from telemetry_common.dtc import InvalidDtcError, decode_dtc, encode_dtc
from telemetry_common.topics import (
    Channel,
    InvalidTopicError,
    build_topic,
    parse_topic,
    subscription_filter,
)

# --- topics ------------------------------------------------------------------


def test_build_topic_follows_hierarchy() -> None:
    assert build_topic("VH-0001", Channel.TELEMETRY) == "vehicles/VH-0001/telemetry"
    assert build_topic("VH-0001", Channel.STATUS) == "vehicles/VH-0001/status"
    assert build_topic("VH-0001", Channel.DIAGNOSTICS) == "vehicles/VH-0001/diagnostics"


@pytest.mark.parametrize("vehicle_id", ["", "ab", "a/b", "+", "#", "VH 0001", "x" * 65])
def test_build_topic_rejects_unsafe_vehicle_ids(vehicle_id: str) -> None:
    with pytest.raises(InvalidTopicError):
        build_topic(vehicle_id, Channel.TELEMETRY)


def test_subscription_filter_uses_single_level_wildcard() -> None:
    assert subscription_filter(Channel.TELEMETRY) == "vehicles/+/telemetry"


@pytest.mark.parametrize("channel", list(Channel))
def test_parse_topic_is_inverse_of_build_topic(channel: Channel) -> None:
    assert parse_topic(build_topic("VH-0042", channel)) == ("VH-0042", channel)


@pytest.mark.parametrize(
    "topic",
    [
        "vehicles/VH-0001",
        "vehicles/VH-0001/telemetry/extra",
        "fleet/VH-0001/telemetry",
        "vehicles/+/telemetry",
        "vehicles/VH-0001/unknown",
        "/vehicles/VH-0001/telemetry",
    ],
)
def test_parse_topic_rejects_foreign_topics(topic: str) -> None:
    with pytest.raises(InvalidTopicError):
        parse_topic(topic)


# --- DTC ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "packed"),
    [("P0217", (0, 0x0217)), ("C1234", (1, 0x1234)), ("B0000", (2, 0)), ("U3FFF", (3, 0x3FFF))],
)
def test_dtc_round_trip(code: str, packed: tuple[int, int]) -> None:
    assert encode_dtc(code) == packed
    assert decode_dtc(*packed) == code


@pytest.mark.parametrize("code", ["", "P217", "X0217", "p0217", "P0217A", "P02G7"])
def test_encode_rejects_invalid_codes(code: str) -> None:
    with pytest.raises(InvalidDtcError):
        encode_dtc(code)


@pytest.mark.parametrize(("category", "number"), [(-1, 0), (4, 0), (0, -1), (0, 0x10000)])
def test_decode_rejects_out_of_range_values(category: int, number: int) -> None:
    with pytest.raises(InvalidDtcError):
        decode_dtc(category, number)
