"""Store-and-forward: drain the persistent buffer to the broker, retrying on failure."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from edge_gateway.backoff import ExponentialBackoff
from edge_gateway.buffer import SqliteBuffer
from edge_gateway.transport import Transport

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FlushResult:
    sent: int
    failed: bool
    connected: bool


class StoreAndForwarder:
    """Sends buffered messages oldest-first; a message leaves the buffer only after its ack.

    Order is preserved: on the first failure the batch is aborted, so a later message
    never overtakes an earlier one that is still waiting for a retry.
    """

    def __init__(
        self,
        buffer: SqliteBuffer,
        transport: Transport,
        backoff: ExponentialBackoff,
        *,
        batch_size: int = 50,
        ack_timeout_s: float = 5.0,
        idle_interval_s: float = 1.0,
    ) -> None:
        self._buffer = buffer
        self._transport = transport
        self._backoff = backoff
        self._batch_size = batch_size
        self._ack_timeout_s = ack_timeout_s
        self._idle_interval_s = idle_interval_s
        self.sent_total = 0
        self.failed_total = 0

    def flush_once(self) -> FlushResult:
        if not self._transport.connected:
            return FlushResult(sent=0, failed=False, connected=False)

        sent = 0
        for message in self._buffer.peek(self._batch_size):
            if self._transport.publish(
                message.topic, message.payload, timeout_s=self._ack_timeout_s
            ):
                self._buffer.delete([message.id])
                sent += 1
            else:
                self.failed_total += 1
                logger.warning("publish_failed", extra={"topic": message.topic, "id": message.id})
                self.sent_total += sent
                return FlushResult(sent=sent, failed=True, connected=self._transport.connected)
        self.sent_total += sent
        return FlushResult(sent=sent, failed=False, connected=True)

    def step(self) -> float:
        """Run one flush and return the number of seconds to wait before the next one."""
        result = self.flush_once()
        if result.sent:
            self._backoff.reset()
        if len(self._buffer) == 0:
            self._backoff.reset()
            return self._idle_interval_s
        if not result.connected or result.failed:
            return self._backoff.next_delay()
        return 0.0  # connected, more batches pending


class ForwardWorker(threading.Thread):
    """Background thread that calls :meth:`StoreAndForwarder.step` in a loop.

    Runs separately from the CAN thread so a slow or dead network can never make the
    gateway miss CAN frames.
    """

    def __init__(self, forwarder: StoreAndForwarder) -> None:
        super().__init__(name="forwarder", daemon=True)
        self._forwarder = forwarder
        self._stop_event = threading.Event()
        self._wake = threading.Event()

    def wake(self) -> None:
        """Tell the worker that new data is waiting (cuts short an idle wait)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                delay = self._forwarder.step()
            except Exception:
                logger.exception("forwarder_error")
                delay = 1.0
            if delay > 0:
                self._wake.wait(delay)
                self._wake.clear()
