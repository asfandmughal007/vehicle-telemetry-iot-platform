from __future__ import annotations

import json

import can
import pytest

from edge_gateway.buffer import SqliteBuffer
from edge_gateway.can_decoder import CanDecoder
from edge_gateway.gateway import EdgeGateway, GatewayPipeline
from edge_gateway.message_builder import MessageBuilder
from edge_gateway.signal_cache import SignalCache
from gw_helpers import FIXED_TIME, FakeClock, make_frame, vehicle_frames
from telemetry_common.can_db import FrameName
from telemetry_common.schema import DiagnosticEvent, TelemetryMessage


class Rig:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.buffer = SqliteBuffer(":memory:", 1000)
        self.pipeline = GatewayPipeline(
            CanDecoder(),
            SignalCache(3.0, self.clock),
            MessageBuilder(
                vehicle_id="VH-0001",
                gateway_id="gw-VH-0001",
                firmware_version="0.1.0",
                clock=lambda: FIXED_TIME,
            ),
            self.buffer,
            vehicle_id="VH-0001",
        )

    def feed(self, **kwargs: object) -> None:
        for frame in vehicle_frames(**kwargs):  # type: ignore[arg-type]
            self.pipeline.handle_frame(frame)

    def queued(self) -> list[tuple[str, dict[str, object]]]:
        return [(m.topic, json.loads(m.payload)) for m in self.buffer.peek(1000)]


@pytest.fixture
def rig() -> Rig:
    return Rig()


# --- pipeline ----------------------------------------------------------------


def test_tick_queues_validated_telemetry_on_the_telemetry_topic(rig: Rig) -> None:
    rig.feed()

    assert rig.pipeline.publish_tick()

    ((topic, payload),) = rig.queued()
    assert topic == "vehicles/VH-0001/telemetry"
    message = TelemetryMessage.model_validate(payload)
    assert message.speed == pytest.approx(50.0, abs=0.01)
    assert message.device.gateway_id == "gw-VH-0001"


def test_no_snapshot_before_all_signals_arrived(rig: Rig) -> None:
    rig.pipeline.handle_frame(vehicle_frames()[0])  # only EngineData

    assert not rig.pipeline.publish_tick()
    assert rig.pipeline.stats.snapshots_incomplete == 1
    assert len(rig.buffer) == 0


def test_stale_signals_stop_publishing_and_recover(rig: Rig) -> None:
    rig.feed()
    assert rig.pipeline.publish_tick()

    rig.clock.now += 10  # CAN bus went silent
    assert not rig.pipeline.publish_tick()
    assert not rig.pipeline.publish_tick()
    assert rig.pipeline.stats.snapshots_incomplete == 2

    rig.feed()
    assert rig.pipeline.publish_tick()
    assert len(rig.buffer) == 2


def test_implausible_value_is_rejected_not_published(rig: Rig) -> None:
    rig.feed(speed=400.0)  # encodes fine on CAN (max 655 km/h) but violates the contract

    assert not rig.pipeline.publish_tick()
    assert rig.pipeline.stats.snapshots_rejected == 1
    assert len(rig.buffer) == 0


def test_malformed_and_unknown_frames_are_counted_and_do_not_disturb(rig: Rig) -> None:
    rig.feed()
    rig.pipeline.handle_frame(can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False))
    rig.pipeline.handle_frame(can.Message(arbitration_id=0x7E8, data=b"\x00", is_extended_id=False))

    stats = rig.pipeline.stats
    assert (stats.frames_malformed, stats.frames_ignored, stats.frames_received) == (1, 1, 7)
    assert rig.pipeline.publish_tick()


def test_dtc_lifecycle_produces_events_on_the_diagnostics_topic(rig: Rig) -> None:
    rig.feed()
    rig.pipeline.publish_tick()  # no DTC -> no event
    rig.feed(dtc="P0217")
    rig.pipeline.publish_tick()  # set
    rig.pipeline.publish_tick()  # unchanged -> no new event
    rig.feed(dtc="P0300")
    rig.pipeline.publish_tick()  # replaced: P0217 cleared, P0300 set
    rig.feed()
    rig.pipeline.publish_tick()  # cleared

    events = [
        DiagnosticEvent.model_validate(p)
        for topic, p in rig.queued()
        if topic == "vehicles/VH-0001/diagnostics"
    ]

    assert [(e.event.value, e.diagnostic_code) for e in events] == [
        ("DTC_SET", "P0217"),
        ("DTC_CLEARED", "P0217"),
        ("DTC_SET", "P0300"),
        ("DTC_CLEARED", "P0300"),
    ]
    assert rig.pipeline.stats.dtc_events == 4


def test_individual_signal_update_changes_next_snapshot(rig: Rig) -> None:
    rig.feed()
    rig.pipeline.publish_tick()
    rig.pipeline.handle_frame(
        make_frame(FrameName.VEHICLE_DYNAMICS, {"VehicleSpeed": 80.0, "FuelLevel": 59.6})
    )
    rig.pipeline.publish_tick()

    speeds = [p["speed"] for _, p in rig.queued()]
    assert speeds[0] == pytest.approx(50.0, abs=0.01)
    assert speeds[1] == pytest.approx(80.0, abs=0.01)


# --- gateway loop ------------------------------------------------------------


class FakeBus:
    """Yields queued frames, then ``idle_polls`` timeouts, then asks the gateway to stop."""

    def __init__(self, frames: list[can.Message], idle_polls: int = 15) -> None:
        self.frames = list(frames)
        self.idle_polls = idle_polls
        self.fail_once = False
        self.gateway: EdgeGateway | None = None
        self.shutdown_called = False

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if self.fail_once:
            self.fail_once = False
            raise can.CanOperationError("bus glitch")
        if self.frames:
            return self.frames.pop(0)
        self.idle_polls -= 1
        if self.idle_polls <= 0 and self.gateway is not None:
            self.gateway.stop()
        return None

    def shutdown(self) -> None:
        self.shutdown_called = True


def run_gateway(rig: Rig, bus: FakeBus) -> list[int]:
    wakes: list[int] = []
    gateway = EdgeGateway(
        bus,  # type: ignore[arg-type]
        rig.pipeline,
        publish_hz=10,
        on_enqueue=lambda: wakes.append(1),
        clock=FakeClock(start=1000.0, tick=0.05),
    )
    bus.gateway = gateway
    gateway.run()
    return wakes


def test_gateway_loop_publishes_periodically_and_notifies_forwarder(rig: Rig) -> None:
    rig.clock = rig.pipeline._cache._clock = FakeClock(start=1000.0, tick=0.05)  # type: ignore[attr-defined]
    bus = FakeBus(vehicle_frames())

    wakes = run_gateway(rig, bus)

    assert bus.shutdown_called
    assert rig.pipeline.stats.snapshots_published >= 1
    assert len(wakes) == rig.pipeline.stats.snapshots_published
    assert len(rig.buffer) == rig.pipeline.stats.snapshots_published


def test_gateway_loop_survives_can_receive_errors(rig: Rig) -> None:
    rig.pipeline._cache._clock = FakeClock(start=1000.0, tick=0.05)  # type: ignore[attr-defined]
    bus = FakeBus(vehicle_frames())
    bus.fail_once = True

    run_gateway(rig, bus)

    assert bus.shutdown_called
    assert rig.pipeline.stats.frames_received == 5
