from __future__ import annotations

import io
import json
from dataclasses import replace
from typing import Any

import can
import pytest

from sim_helpers import FIXED_TIME
from telemetry_common.can_db import RAW_TO_ENGINE_STATUS, FrameName, load_vehicle_dbc
from telemetry_common.dtc import decode_dtc
from telemetry_common.schema import EngineStatus
from vehicle_simulator.can_encoder import CanFrameEncoder, CyclicFrameScheduler
from vehicle_simulator.model import VehicleSnapshot
from vehicle_simulator.sinks import CanBusSink, StdoutSink

SNAPSHOT = VehicleSnapshot(
    vehicle_id="VH-TEST",
    timestamp=FIXED_TIME,
    speed=87.42,
    rpm=2150.25,
    coolant_temperature=91.0,
    battery_voltage=14.112,
    fuel_level=63.2,
    latitude=49.0134123,
    longitude=12.1016456,
    engine_status=EngineStatus.RUNNING,
    diagnostic_code="P0217",
)


def decode_all(frames: list[can.Message]) -> dict[str, Any]:
    db = load_vehicle_dbc()
    signals: dict[str, Any] = {}
    for frame in frames:
        signals.update(db.decode_message(frame.arbitration_id, frame.data, decode_choices=False))
    return signals


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# --- encoder -----------------------------------------------------------------


def test_encode_produces_one_standard_frame_per_dbc_message() -> None:
    frames = CanFrameEncoder().encode(SNAPSHOT)

    assert [f.arbitration_id for f in frames] == [0x100, 0x200, 0x300, 0x400, 0x500]
    assert all(f.dlc == 8 and not f.is_extended_id for f in frames)


def test_encoded_frames_decode_back_within_signal_resolution() -> None:
    signals = decode_all(CanFrameEncoder().encode(SNAPSHOT))

    assert signals["VehicleSpeed"] == pytest.approx(SNAPSHOT.speed, abs=0.01)
    assert signals["EngineSpeed"] == pytest.approx(SNAPSHOT.rpm, abs=0.25)
    assert signals["CoolantTemperature"] == pytest.approx(SNAPSHOT.coolant_temperature, abs=1.0)
    assert signals["BatteryVoltage"] == pytest.approx(SNAPSHOT.battery_voltage, abs=0.001)
    assert signals["FuelLevel"] == pytest.approx(SNAPSHOT.fuel_level, abs=0.4)
    assert signals["Latitude"] == pytest.approx(SNAPSHOT.latitude, abs=1e-7)
    assert signals["Longitude"] == pytest.approx(SNAPSHOT.longitude, abs=1e-7)
    assert RAW_TO_ENGINE_STATUS[signals["EngineStatus"]] is EngineStatus.RUNNING
    assert signals["DtcActive"] == 1
    assert decode_dtc(signals["DtcCategory"], signals["DtcCode"]) == "P0217"


def test_no_dtc_is_encoded_as_inactive() -> None:
    signals = decode_all(CanFrameEncoder().encode(replace(SNAPSHOT, diagnostic_code=None)))

    assert (signals["DtcActive"], signals["DtcCategory"], signals["DtcCode"]) == (0, 0, 0)


def test_encode_can_be_limited_to_selected_frames() -> None:
    frames = CanFrameEncoder().encode(SNAPSHOT, [FrameName.GNSS_POSITION])

    assert [f.arbitration_id for f in frames] == [0x400]


def test_out_of_range_signal_raises_instead_of_wrapping() -> None:
    with pytest.raises(Exception, match="VehicleSpeed"):
        CanFrameEncoder().encode(replace(SNAPSHOT, speed=700.0))


# --- scheduler ---------------------------------------------------------------


def test_scheduler_sends_everything_first_then_respects_cycle_times() -> None:
    scheduler = CyclicFrameScheduler(CanFrameEncoder().cycle_times_s())
    counts = dict.fromkeys(FrameName, 0)

    for tick in range(100):  # 10 s at 10 Hz
        for frame in scheduler.due(tick * 0.1):
            counts[frame] += 1

    assert counts[FrameName.ENGINE_DATA] == 100
    assert counts[FrameName.VEHICLE_DYNAMICS] == 100
    assert counts[FrameName.BATTERY_STATUS] == 10
    assert counts[FrameName.GNSS_POSITION] == 10
    assert counts[FrameName.DIAGNOSTIC_TROUBLE_CODE] == 10


def test_scheduler_does_not_burst_after_a_stall() -> None:
    scheduler = CyclicFrameScheduler({FrameName.ENGINE_DATA: 0.1})
    assert scheduler.due(0.0) == [FrameName.ENGINE_DATA]

    assert scheduler.due(50.0) == [FrameName.ENGINE_DATA]  # one frame, not 500
    assert scheduler.due(50.05) == []
    assert scheduler.due(50.1) == [FrameName.ENGINE_DATA]


def test_scheduler_rejects_non_positive_cycle_time() -> None:
    with pytest.raises(ValueError, match="cycle time"):
        CyclicFrameScheduler({FrameName.ENGINE_DATA: 0.0})


# --- sinks -------------------------------------------------------------------


def test_can_bus_sink_delivers_frames_over_a_virtual_bus() -> None:
    clock = FakeClock()
    channel = "test-can-sink"
    with can.Bus(interface="virtual", channel=channel) as receiver:
        sink = CanBusSink(can.Bus(interface="virtual", channel=channel), clock=clock)
        sink.send(SNAPSHOT)
        clock.now += 0.1
        sink.send(SNAPSHOT)

        received = []
        while (frame := receiver.recv(timeout=0.2)) is not None:
            received.append(frame)
        sink.close()

    # first tick: all 5 frames; second tick (100 ms later): only the two fast frames
    assert [f.arbitration_id for f in received] == [0x100, 0x200, 0x300, 0x400, 0x500, 0x100, 0x200]
    assert sink.frames_sent == 7
    assert sink.send_errors == 0
    assert decode_all(received)["VehicleSpeed"] == pytest.approx(SNAPSHOT.speed, abs=0.01)


def test_can_bus_sink_counts_send_errors_and_keeps_going() -> None:
    class FailingBus:
        def __init__(self) -> None:
            self.shutdown_called = False

        def send(self, msg: can.Message, timeout: float | None = None) -> None:
            raise can.CanOperationError("bus off")

        def shutdown(self) -> None:
            self.shutdown_called = True

    bus = FailingBus()
    sink = CanBusSink(bus, clock=FakeClock())  # type: ignore[arg-type]

    sink.send(SNAPSHOT)
    sink.close()

    assert sink.send_errors == 5
    assert sink.frames_sent == 0
    assert bus.shutdown_called


def test_stdout_sink_writes_rate_limited_json_lines() -> None:
    clock = FakeClock()
    stream = io.StringIO()
    sink = StdoutSink(interval_s=1.0, stream=stream, clock=clock)

    for _ in range(25):  # 2.5 s at 10 Hz
        sink.send(SNAPSHOT)
        clock.now += 0.1
    sink.close()

    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["vehicle_id"] == "VH-TEST"
    assert json.loads(lines[0])["diagnostic_code"] == "P0217"
