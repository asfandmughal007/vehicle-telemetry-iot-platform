"""Entry point: ``python -m vehicle_simulator``."""

from __future__ import annotations

import logging
import random
import signal
import sys
from types import FrameType

import can
from pydantic import ValidationError

from telemetry_common.structured_logging import configure_logging
from vehicle_simulator.config import OutputMode, SimulatorSettings
from vehicle_simulator.model import VehicleModel
from vehicle_simulator.runner import SimulatorRunner
from vehicle_simulator.sinks import CanBusSink, StdoutSink, TelemetrySink

logger = logging.getLogger("vehicle_simulator")

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2


def build_sink(settings: SimulatorSettings) -> TelemetrySink:
    """Create the output adapter selected by configuration."""
    if settings.output is OutputMode.STDOUT:
        return StdoutSink()
    bus = can.Bus(interface=settings.can_interface, channel=settings.can_channel)
    return CanBusSink(bus)


def _install_signal_handlers(runner: SimulatorRunner) -> None:
    def handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("shutdown_requested", extra={"signal": signal.Signals(signum).name})
        runner.stop()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)  # sent by `docker stop`


def main() -> int:
    try:
        settings = SimulatorSettings()
    except ValidationError as exc:
        print(f"Invalid simulator configuration:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    configure_logging("vehicle-simulator", settings.log_level, json_output=settings.log_json)

    model = VehicleModel(
        settings.vehicle_id,
        start_latitude=settings.start_latitude,
        start_longitude=settings.start_longitude,
        rng=random.Random(settings.seed),
        fault_rate_per_hour=settings.fault_rate_per_hour,
    )

    try:
        sink = build_sink(settings)
    except (can.CanError, OSError, ValueError, NotImplementedError) as exc:
        logger.error(
            "can_bus_unavailable",
            extra={
                "interface": settings.can_interface,
                "channel": settings.can_channel,
                "error": str(exc),
            },
        )
        return EXIT_RUNTIME_ERROR

    runner = SimulatorRunner(model, sink, tick_hz=settings.tick_hz, duration_s=settings.duration_s)
    _install_signal_handlers(runner)
    runner.run()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
