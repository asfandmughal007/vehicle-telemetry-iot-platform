"""Output adapters ("sinks") for vehicle snapshots.

The simulation loop only knows the :class:`TelemetrySink` protocol; whether data
ends up on a CAN bus or on stdout is decided at start-up from configuration.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from typing import Protocol, TextIO

import can

from vehicle_simulator.can_encoder import CanFrameEncoder, CyclicFrameScheduler
from vehicle_simulator.model import VehicleSnapshot

logger = logging.getLogger(__name__)

_CAN_SEND_TIMEOUT_S = 0.1


class TelemetrySink(Protocol):
    """Destination for vehicle snapshots."""

    def send(self, snapshot: VehicleSnapshot) -> None: ...

    def close(self) -> None: ...


class CanBusSink:
    """Sends snapshots as cyclic CAN frames on a python-can bus.

    A failed transmission is counted and logged but never stops the simulator:
    an ECU keeps sending on its cycle regardless of whether anyone is listening.
    """

    def __init__(
        self,
        bus: can.BusABC,
        encoder: CanFrameEncoder | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._encoder = encoder if encoder is not None else CanFrameEncoder()
        self._scheduler = CyclicFrameScheduler(self._encoder.cycle_times_s())
        self._clock = clock
        self.frames_sent = 0
        self.send_errors = 0

    def send(self, snapshot: VehicleSnapshot) -> None:
        due = self._scheduler.due(self._clock())
        if not due:
            return
        for frame in self._encoder.encode(snapshot, due):
            try:
                self._bus.send(frame, timeout=_CAN_SEND_TIMEOUT_S)
            except can.CanError as exc:
                self.send_errors += 1
                logger.warning(
                    "can_send_failed",
                    extra={"arbitration_id": hex(frame.arbitration_id), "error": str(exc)},
                )
            else:
                self.frames_sent += 1

    def close(self) -> None:
        self._bus.shutdown()
        logger.info(
            "can_bus_closed",
            extra={"frames_sent": self.frames_sent, "send_errors": self.send_errors},
        )


class StdoutSink:
    """Prints snapshots as JSON lines, rate-limited -- for demos without a CAN bus."""

    def __init__(
        self,
        *,
        interval_s: float = 1.0,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval_s = interval_s
        self._stream = stream if stream is not None else sys.stdout
        self._clock = clock
        self._next_due: float | None = None

    def send(self, snapshot: VehicleSnapshot) -> None:
        now = self._clock()
        if self._next_due is not None and now < self._next_due:
            return
        self._next_due = now + self._interval_s
        self._stream.write(json.dumps(snapshot.to_dict()) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.flush()
