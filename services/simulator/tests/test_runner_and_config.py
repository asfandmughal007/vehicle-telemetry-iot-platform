from __future__ import annotations

import pytest
from pydantic import ValidationError

from vehicle_simulator.__main__ import EXIT_CONFIG_ERROR, EXIT_OK, build_sink, main
from vehicle_simulator.config import OutputMode, SimulatorSettings
from vehicle_simulator.model import VehicleModel, VehicleSnapshot
from vehicle_simulator.runner import SimulatorRunner
from vehicle_simulator.sinks import CanBusSink, StdoutSink


class RecordingSink:
    def __init__(self) -> None:
        self.snapshots: list[VehicleSnapshot] = []
        self.closed = False

    def send(self, snapshot: VehicleSnapshot) -> None:
        self.snapshots.append(snapshot)

    def close(self) -> None:
        self.closed = True


class FakeTime:
    """Monotonic clock that only advances when the runner waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


# --- runner ------------------------------------------------------------------


def test_runner_executes_fixed_number_of_ticks_for_duration(model: VehicleModel) -> None:
    sink, time = RecordingSink(), FakeTime()
    runner = SimulatorRunner(
        model, sink, tick_hz=10, duration_s=2.0, clock=time.clock, wait=time.wait
    )

    ticks = runner.run()

    assert ticks == len(sink.snapshots) == 20
    assert sink.closed
    assert all(wait == pytest.approx(0.1) for wait in time.waits)


def test_runner_stops_when_requested(model: VehicleModel) -> None:
    sink, time = RecordingSink(), FakeTime()
    runner: SimulatorRunner

    def wait_then_stop(seconds: float) -> None:
        time.wait(seconds)
        if len(sink.snapshots) == 5:
            runner.stop()

    runner = SimulatorRunner(model, sink, tick_hz=10, clock=time.clock, wait=wait_then_stop)

    assert runner.run() == 5
    assert sink.closed


def test_runner_closes_sink_even_if_sink_fails(model: VehicleModel) -> None:
    class BrokenSink(RecordingSink):
        def send(self, snapshot: VehicleSnapshot) -> None:
            raise RuntimeError("sink failure")

    sink, time = BrokenSink(), FakeTime()
    runner = SimulatorRunner(model, sink, tick_hz=10, clock=time.clock, wait=time.wait)

    with pytest.raises(RuntimeError, match="sink failure"):
        runner.run()
    assert sink.closed


def test_runner_resynchronises_instead_of_bursting_after_stall(model: VehicleModel) -> None:
    sink, time = RecordingSink(), FakeTime()

    class StallingSink(RecordingSink):
        def send(self, snapshot: VehicleSnapshot) -> None:
            super().send(snapshot)
            if len(self.snapshots) == 1:
                time.now += 60.0  # host suspended for a minute

    sink = StallingSink()
    runner = SimulatorRunner(
        model, sink, tick_hz=10, duration_s=61.0, clock=time.clock, wait=time.wait
    )

    ticks = runner.run()

    assert ticks < 20  # ~1 s of ticks after the stall, not 600 catch-up ticks


def test_runner_rejects_invalid_tick_rate(model: VehicleModel) -> None:
    with pytest.raises(ValueError, match="tick_hz"):
        SimulatorRunner(model, RecordingSink(), tick_hz=0)


# --- configuration -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    import os

    for key in list(os.environ):
        if key.startswith("SIM_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(str(tmp_path))  # no stray .env file


def test_settings_defaults() -> None:
    settings = SimulatorSettings()

    assert settings.vehicle_id == "VH-0001"
    assert settings.output is OutputMode.CAN
    assert settings.tick_hz == 10.0
    assert settings.seed is None
    assert settings.duration_s is None


def test_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_VEHICLE_ID", "VH-0042")
    monkeypatch.setenv("SIM_OUTPUT", "stdout")
    monkeypatch.setenv("SIM_TICK_HZ", "20")
    monkeypatch.setenv("SIM_SEED", "123")

    settings = SimulatorSettings()

    assert settings.vehicle_id == "VH-0042"
    assert settings.output is OutputMode.STDOUT
    assert settings.tick_hz == 20.0
    assert settings.seed == 123


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SIM_VEHICLE_ID", "bad/id"),
        ("SIM_TICK_HZ", "0"),
        ("SIM_OUTPUT", "kafka"),
        ("SIM_START_LATITUDE", "91"),
        ("SIM_LOG_LEVEL", "LOUD"),
        ("SIM_FAULT_RATE_PER_HOUR", "-1"),
    ],
)
def test_invalid_settings_fail_fast(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        SimulatorSettings()


# --- entry point -------------------------------------------------------------


def test_build_sink_selects_adapter_from_settings() -> None:
    assert isinstance(build_sink(SimulatorSettings(output=OutputMode.STDOUT)), StdoutSink)

    can_sink = build_sink(SimulatorSettings(can_interface="virtual", can_channel="test-build"))
    try:
        assert isinstance(can_sink, CanBusSink)
    finally:
        can_sink.close()


def test_main_runs_for_configured_duration_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SIM_OUTPUT", "stdout")
    monkeypatch.setenv("SIM_DURATION_S", "0.3")
    monkeypatch.setenv("SIM_TICK_HZ", "50")
    monkeypatch.setenv("SIM_SEED", "1")

    assert main() == EXIT_OK

    out = capsys.readouterr().out
    assert '"vehicle_id": "VH-0001"' in out


def test_main_returns_config_error_for_invalid_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SIM_TICK_HZ", "-5")

    assert main() == EXIT_CONFIG_ERROR
    assert "Invalid simulator configuration" in capsys.readouterr().err


def test_empty_environment_variable_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker-compose renders "${SIM_SEED:-}" as an empty string
    monkeypatch.setenv("SIM_SEED", "")

    assert SimulatorSettings().seed is None
