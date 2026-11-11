"""Fixed-rate simulation loop."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from vehicle_simulator.model import VehicleModel
from vehicle_simulator.sinks import TelemetrySink

logger = logging.getLogger(__name__)

#: If the loop falls this many ticks behind (e.g. the host was suspended), drop the
#: backlog and resynchronise instead of running flat out to catch up.
_MAX_BACKLOG_TICKS = 10


class SimulatorRunner:
    """Steps the vehicle model at a fixed rate and forwards snapshots to a sink.

    The model always advances by exactly ``1 / tick_hz`` seconds per tick, so
    simulated behaviour does not depend on scheduling jitter. Deadlines are
    absolute (``next_tick += period``), which avoids the cumulative drift of a
    naive ``sleep(period)`` loop.

    Args:
        model: Vehicle model to advance.
        sink: Receives every snapshot.
        tick_hz: Update rate in Hz.
        duration_s: Stop automatically after this many seconds (``None`` = run until stopped).
        status_interval_s: Period of the ``simulator_status`` log line.
        clock: Monotonic time source; injectable for tests.
        wait: Blocks for up to N seconds, returns early when stopped; injectable for tests.
    """

    def __init__(
        self,
        model: VehicleModel,
        sink: TelemetrySink,
        *,
        tick_hz: float,
        duration_s: float | None = None,
        status_interval_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], object] | None = None,
    ) -> None:
        if tick_hz <= 0:
            raise ValueError("tick_hz must be > 0")
        self._model = model
        self._sink = sink
        self._period_s = 1.0 / tick_hz
        self._duration_s = duration_s
        self._status_interval_s = status_interval_s
        self._clock = clock
        self._stop_event = threading.Event()
        self._wait = wait if wait is not None else self._stop_event.wait

    def stop(self) -> None:
        """Request a graceful stop; safe to call from a signal handler or another thread."""
        self._stop_event.set()

    def run(self) -> int:
        """Run until stopped or ``duration_s`` elapsed. Returns the number of ticks executed."""
        start = self._clock()
        next_tick = start
        last_status = start
        ticks = 0
        logger.info(
            "simulator_started",
            extra={"vehicle_id": self._model.vehicle_id, "tick_hz": round(1 / self._period_s, 2)},
        )
        try:
            while not self._stop_event.is_set():
                now = self._clock()
                if self._duration_s is not None and now - start >= self._duration_s:
                    break

                snapshot = self._model.step(self._period_s)
                self._sink.send(snapshot)
                ticks += 1

                if now - last_status >= self._status_interval_s:
                    last_status = now
                    logger.info("simulator_status", extra={"ticks": ticks, **snapshot.to_dict()})

                next_tick += self._period_s
                delay = next_tick - self._clock()
                if delay < -_MAX_BACKLOG_TICKS * self._period_s:
                    logger.warning("simulator_overrun", extra={"behind_s": round(-delay, 3)})
                    next_tick = self._clock()
                elif delay > 0:
                    self._wait(delay)
        finally:
            self._sink.close()
            logger.info(
                "simulator_stopped", extra={"vehicle_id": self._model.vehicle_id, "ticks": ticks}
            )
        return ticks
