from __future__ import annotations

import can
import pytest
from pydantic import ValidationError

from edge_gateway.can_decoder import CanDecoder, FrameDecodeError
from edge_gateway.message_builder import MessageBuilder
from edge_gateway.signal_cache import REQUIRED_FIELDS, IncompleteSnapshotError, SignalCache
from gw_helpers import FIXED_TIME, FakeClock, as_dict, vehicle_frames
from telemetry_common.schema import (
    ConnectionStatus,
    DiagnosticEventType,
    EngineStatus,
    TelemetryMessage,
)

# --- decoder -----------------------------------------------------------------


def decode_all(**kwargs: object) -> dict[str, object]:
    decoder = CanDecoder()
    fields: dict[str, object] = {}
    for frame in vehicle_frames(**kwargs):  # type: ignore[arg-type]
        decoded = decoder.decode(frame)
        assert decoded is not None
        fields.update(decoded)
    return fields


def test_decoder_returns_physical_values_for_all_fields() -> None:
    fields = decode_all()

    assert set(fields) == set(REQUIRED_FIELDS)
    assert fields["speed"] == pytest.approx(50.0, abs=0.01)
    assert fields["rpm"] == pytest.approx(2000.0, abs=0.25)
    assert fields["coolant_temperature"] == pytest.approx(90.0)
    assert fields["battery_voltage"] == pytest.approx(14.1, abs=0.001)
    assert fields["fuel_level"] == pytest.approx(60.0, abs=0.4)
    assert fields["latitude"] == pytest.approx(49.0134, abs=1e-7)
    assert fields["longitude"] == pytest.approx(12.1016, abs=1e-7)
    assert fields["engine_status"] is EngineStatus.RUNNING
    assert fields["diagnostic_code"] is None


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (0, EngineStatus.OFF),
        (1, EngineStatus.IDLE),
        (2, EngineStatus.RUNNING),
        (3, EngineStatus.FAULT),
    ],
)
def test_decoder_maps_engine_status(raw: int, status: EngineStatus) -> None:
    assert decode_all(engine_status_raw=raw)["engine_status"] is status


def test_decoder_reports_active_dtc_as_text() -> None:
    assert decode_all(dtc="P0217")["diagnostic_code"] == "P0217"
    assert decode_all(dtc="U3FFF")["diagnostic_code"] == "U3FFF"


def test_decoder_ignores_unknown_frames() -> None:
    frame = can.Message(arbitration_id=0x7DF, data=b"\x02\x01\x0c", is_extended_id=False)

    assert CanDecoder().decode(frame) is None


def test_decoder_rejects_truncated_frame() -> None:
    frame = can.Message(arbitration_id=0x100, data=b"\x01\x02", is_extended_id=False)

    with pytest.raises(FrameDecodeError):
        CanDecoder().decode(frame)


def test_decoder_rejects_remote_and_error_frames() -> None:
    remote = can.Message(arbitration_id=0x100, is_remote_frame=True, is_extended_id=False)
    error = can.Message(arbitration_id=0x100, is_error_frame=True, is_extended_id=False)

    for frame in (remote, error):
        with pytest.raises(FrameDecodeError):
            CanDecoder().decode(frame)


# --- signal cache ------------------------------------------------------------


def test_cache_requires_all_signals() -> None:
    cache = SignalCache(3.0, FakeClock())
    cache.update(as_dict())
    partial = SignalCache(3.0, FakeClock())
    partial.update({"speed": 1.0})

    assert cache.snapshot()["speed"] == 50.0
    with pytest.raises(IncompleteSnapshotError) as exc_info:
        partial.snapshot()
    assert "rpm" in exc_info.value.missing


def test_cache_treats_none_diagnostic_code_as_a_present_value() -> None:
    cache = SignalCache(3.0, FakeClock())
    cache.update(as_dict(diagnostic_code=None))

    assert cache.snapshot()["diagnostic_code"] is None


def test_cache_reports_stale_signals_and_recovers_on_update() -> None:
    clock = FakeClock()
    cache = SignalCache(3.0, clock)
    cache.update(as_dict())

    clock.now += 3.5
    with pytest.raises(IncompleteSnapshotError) as exc_info:
        cache.snapshot()
    assert set(exc_info.value.stale) == set(REQUIRED_FIELDS)

    cache.update({"speed": 70.0})  # only one signal refreshed
    with pytest.raises(IncompleteSnapshotError) as exc_info:
        cache.snapshot()
    assert exc_info.value.stale == [f for f in REQUIRED_FIELDS if f != "speed"]

    cache.update(as_dict(speed=70.0))
    assert cache.snapshot()["speed"] == 70.0


def test_cache_rejects_invalid_max_age() -> None:
    with pytest.raises(ValueError, match="max_age_s"):
        SignalCache(0.0)


# --- message builder ---------------------------------------------------------


@pytest.fixture
def builder() -> MessageBuilder:
    return MessageBuilder(
        vehicle_id="VH-0001",
        gateway_id="gw-VH-0001",
        firmware_version="1.2.3",
        clock=lambda: FIXED_TIME,
    )


def test_builder_adds_identity_timestamp_and_device_metadata(builder: MessageBuilder) -> None:
    message = builder.build_telemetry(as_dict(diagnostic_code="P0217"))

    assert isinstance(message, TelemetryMessage)
    assert message.vehicle_id == "VH-0001"
    assert message.timestamp == FIXED_TIME
    assert message.device.gateway_id == "gw-VH-0001"
    assert message.device.firmware_version == "1.2.3"
    assert message.diagnostic_code == "P0217"


def test_builder_sequence_increments_and_ids_are_unique(builder: MessageBuilder) -> None:
    first, second = builder.build_telemetry(as_dict()), builder.build_telemetry(as_dict())

    assert (first.device.sequence, second.device.sequence) == (0, 1)
    assert first.message_id != second.message_id


def test_builder_rejects_implausible_values_without_consuming_a_sequence(
    builder: MessageBuilder,
) -> None:
    with pytest.raises(ValidationError):
        builder.build_telemetry(as_dict(speed=400.0))  # fits the CAN signal, not the contract

    assert builder.build_telemetry(as_dict()).device.sequence == 0


def test_builder_creates_diagnostic_event_and_status(builder: MessageBuilder) -> None:
    event = builder.build_diagnostic_event(DiagnosticEventType.DTC_SET, "P0300")
    status = builder.build_status(ConnectionStatus.ONLINE)

    assert (event.event, event.diagnostic_code) == (DiagnosticEventType.DTC_SET, "P0300")
    assert status.status is ConnectionStatus.ONLINE
    assert status.vehicle_id == "VH-0001"
    with pytest.raises(ValidationError):
        builder.build_diagnostic_event(DiagnosticEventType.DTC_SET, "bad")
