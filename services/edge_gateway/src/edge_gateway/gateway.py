"""Gateway core: frames in, validated messages into the persistent buffer."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import can
from pydantic import BaseModel, ValidationError

from edge_gateway.buffer import SqliteBuffer
from edge_gateway.can_decoder import CanDecoder, FrameDecodeError
from edge_gateway.message_builder import MessageBuilder
from edge_gateway.signal_cache import IncompleteSnapshotError, SignalCache
from telemetry_common.schema import DiagnosticEventType
from telemetry_common.topics import Channel, build_topic

logger = logging.getLogger(__name__)

_CAN_RECV_TIMEOUT_S = 0.1
_CAN_ERROR_PAUSE_S = 0.5


@dataclass(slots=True)
class GatewayStats:
    frames_received: int = 0
    frames_ignored: int = 0
    frames_malformed: int = 0
    snapshots_published: int = 0
    snapshots_incomplete: int = 0
    snapshots_rejected: int = 0
    dtc_events: int = 0


class GatewayPipeline:
    """The gateway logic without I/O loops, so it is fully unit-testable."""

    def __init__(
        self,
        decoder: CanDecoder,
        cache: SignalCache,
        builder: MessageBuilder,
        buffer: SqliteBuffer,
        *,
        vehicle_id: str,
    ) -> None:
        self._decoder = decoder
        self._cache = cache
        self._builder = builder
        self._buffer = buffer
        self._vehicle_id = vehicle_id
        self._telemetry_topic = build_topic(vehicle_id, Channel.TELEMETRY)
        self._diagnostics_topic = build_topic(vehicle_id, Channel.DIAGNOSTICS)
        self._last_dtc: str | None = None
        self._degraded = False
        self.stats = GatewayStats()

    def handle_frame(self, frame: can.Message) -> None:
        self.stats.frames_received += 1
        try:
            fields = self._decoder.decode(frame)
        except FrameDecodeError as exc:
            self.stats.frames_malformed += 1
            logger.warning(
                "can_frame_malformed",
                extra={"arbitration_id": hex(frame.arbitration_id), "error": str(exc)},
            )
            return
        if fields is None:
            self.stats.frames_ignored += 1
            return
        self._cache.update(fields)

    def publish_tick(self) -> bool:
        """Sample the cache, validate, and enqueue. Returns ``True`` if a message was queued."""
        try:
            fields = self._cache.snapshot()
        except IncompleteSnapshotError as exc:
            self.stats.snapshots_incomplete += 1
            if not self._degraded:  # log on state change only, not every second
                logger.warning(
                    "snapshot_unavailable", extra={"missing": exc.missing, "stale": exc.stale}
                )
            self._degraded = True
            return False
        if self._degraded:
            logger.info("snapshot_recovered")
            self._degraded = False

        try:
            message = self._builder.build_telemetry(fields)
        except ValidationError as exc:
            self.stats.snapshots_rejected += 1
            logger.warning(
                "snapshot_rejected",
                extra={
                    "errors": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
                },
            )
            return False

        self._buffer.enqueue(self._telemetry_topic, message.model_dump_json())
        self.stats.snapshots_published += 1
        self._emit_dtc_event(message.diagnostic_code)
        return True

    def _emit_dtc_event(self, current: str | None) -> None:
        previous = self._last_dtc
        if current == previous:
            return
        self._last_dtc = current
        # A change from one code to another is reported as "old cleared, new set".
        changes: list[tuple[DiagnosticEventType, str]] = []
        if previous is not None:
            changes.append((DiagnosticEventType.DTC_CLEARED, previous))
        if current is not None:
            changes.append((DiagnosticEventType.DTC_SET, current))
        for event_type, code in changes:
            event = self._builder.build_diagnostic_event(event_type, code)
            self._enqueue(self._diagnostics_topic, event)
            self.stats.dtc_events += 1
            logger.info("dtc_event", extra={"event": event_type.value, "dtc": code})

    def _enqueue(self, topic: str, message: BaseModel) -> None:
        self._buffer.enqueue(topic, message.model_dump_json())


class EdgeGateway:
    """Runs the CAN receive loop and triggers the periodic publish."""

    def __init__(
        self,
        bus: can.BusABC,
        pipeline: GatewayPipeline,
        *,
        publish_hz: float,
        on_enqueue: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._pipeline = pipeline
        self._period_s = 1.0 / publish_hz
        self._on_enqueue = on_enqueue
        self._clock = clock
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        next_publish = self._clock() + self._period_s
        logger.info("gateway_started", extra={"publish_period_s": self._period_s})
        try:
            while not self._stop_event.is_set():
                timeout = max(0.0, min(_CAN_RECV_TIMEOUT_S, next_publish - self._clock()))
                try:
                    frame = self._bus.recv(timeout=timeout)
                except can.CanError as exc:
                    logger.error("can_receive_failed", extra={"error": str(exc)})
                    self._stop_event.wait(_CAN_ERROR_PAUSE_S)
                    frame = None
                if frame is not None:
                    self._pipeline.handle_frame(frame)

                now = self._clock()
                if now >= next_publish:
                    if self._pipeline.publish_tick():
                        self._on_enqueue()
                    next_publish += self._period_s
                    if next_publish <= now:  # fell behind: resynchronise, do not burst
                        next_publish = now + self._period_s
        finally:
            self._bus.shutdown()
            logger.info("gateway_stopped", extra=asdict(self._pipeline.stats))
