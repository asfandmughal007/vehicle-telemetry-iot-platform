from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from edge_gateway.backoff import ExponentialBackoff
from edge_gateway.buffer import SqliteBuffer
from edge_gateway.forwarder import ForwardWorker, StoreAndForwarder
from gw_helpers import FakeTransport

# --- buffer ------------------------------------------------------------------


def test_buffer_is_fifo_and_peek_does_not_remove() -> None:
    buffer = SqliteBuffer(":memory:", 10)
    for i in range(3):
        buffer.enqueue("t", f"m{i}")

    first = buffer.peek(2)

    assert [m.payload for m in first] == ["m0", "m1"]
    assert len(buffer) == 3
    buffer.delete([first[0].id])
    assert [m.payload for m in buffer.peek(10)] == ["m1", "m2"]


def test_buffer_drops_oldest_when_full() -> None:
    buffer = SqliteBuffer(":memory:", 3)

    dropped = [buffer.enqueue("t", f"m{i}") for i in range(5)]

    assert dropped == [0, 0, 0, 1, 1]
    assert [m.payload for m in buffer.peek(10)] == ["m2", "m3", "m4"]
    assert buffer.dropped_total == 2


def test_buffer_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "buffer.sqlite3"  # parent directory is created
    first = SqliteBuffer(path, 10)
    first.enqueue("vehicles/VH-0001/telemetry", "payload")
    first.close()

    second = SqliteBuffer(path, 10)

    assert [(m.topic, m.payload) for m in second.peek(10)] == [
        ("vehicles/VH-0001/telemetry", "payload")
    ]


def test_buffer_rejects_invalid_capacity() -> None:
    with pytest.raises(ValueError, match="max_messages"):
        SqliteBuffer(":memory:", 0)


# --- backoff -----------------------------------------------------------------


def test_backoff_grows_exponentially_with_jitter_and_is_capped() -> None:
    backoff = ExponentialBackoff(1.0, 10.0, random.Random(1))

    delays = [backoff.next_delay() for _ in range(8)]

    ceilings = [1, 2, 4, 8, 10, 10, 10, 10]
    for delay, ceiling in zip(delays, ceilings, strict=True):
        assert ceiling * 0.5 <= delay <= ceiling


def test_backoff_reset_starts_over() -> None:
    backoff = ExponentialBackoff(1.0, 60.0, random.Random(1))
    for _ in range(5):
        backoff.next_delay()

    backoff.reset()

    assert backoff.next_delay() <= 1.0


def test_backoff_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="base_s"):
        ExponentialBackoff(0.0, 1.0)
    with pytest.raises(ValueError, match="base_s"):
        ExponentialBackoff(5.0, 1.0)


# --- store-and-forward -------------------------------------------------------


def make_forwarder(
    transport: FakeTransport, messages: int = 3, **kwargs: float
) -> tuple[StoreAndForwarder, SqliteBuffer]:
    buffer = SqliteBuffer(":memory:", 100)
    for i in range(messages):
        buffer.enqueue("t", f"m{i}")
    backoff = ExponentialBackoff(1.0, 30.0, random.Random(1))
    return StoreAndForwarder(buffer, transport, backoff, batch_size=2, **kwargs), buffer


def test_nothing_is_sent_or_lost_while_offline() -> None:
    transport = FakeTransport(connected=False)
    forwarder, buffer = make_forwarder(transport)

    result = forwarder.flush_once()

    assert (result.sent, result.failed, result.connected) == (0, False, False)
    assert len(buffer) == 3
    assert transport.published == []


def test_buffered_messages_are_sent_in_order_after_reconnect() -> None:
    transport = FakeTransport(connected=False)
    forwarder, buffer = make_forwarder(transport, messages=5)
    forwarder.flush_once()

    transport.set_connected(True)
    while len(buffer):
        forwarder.flush_once()

    assert [p for _, p in transport.published] == ["m0", "m1", "m2", "m3", "m4"]
    assert forwarder.sent_total == 5


def test_failed_publish_keeps_message_and_preserves_order() -> None:
    transport = FakeTransport()
    transport.results = [True, False]  # m0 acked, m1 not acked
    forwarder, buffer = make_forwarder(transport)

    result = forwarder.flush_once()

    assert (result.sent, result.failed) == (1, True)
    assert [m.payload for m in buffer.peek(10)] == ["m1", "m2"]
    assert forwarder.failed_total == 1

    forwarder.flush_once()  # retry succeeds, m1 still goes before m2
    assert [p for _, p in transport.published] == ["m0", "m1", "m2"][: len(transport.published)]
    assert transport.published[1][1] == "m1"


def test_step_waits_idle_interval_when_buffer_is_empty() -> None:
    forwarder, _ = make_forwarder(FakeTransport(), messages=0, idle_interval_s=1.0)

    assert forwarder.step() == 1.0


def test_step_backs_off_while_disconnected_and_resets_after_success() -> None:
    transport = FakeTransport(connected=False)
    forwarder, _ = make_forwarder(transport, messages=1)

    first, second, third = forwarder.step(), forwarder.step(), forwarder.step()
    assert 0.5 <= first <= 1.0
    assert 1.0 <= second <= 2.0
    assert 2.0 <= third <= 4.0

    transport.set_connected(True)
    assert forwarder.step() == 1.0  # sent the last message, buffer empty -> idle
    transport.set_connected(False)
    forwarder._buffer.enqueue("t", "again")
    assert forwarder.step() <= 1.0  # backoff restarted from the base delay


def test_step_returns_zero_when_more_batches_are_pending() -> None:
    forwarder, _ = make_forwarder(FakeTransport(), messages=5)  # batch size is 2

    assert forwarder.step() == 0.0


def test_step_backs_off_after_failed_publish() -> None:
    transport = FakeTransport(default=False)
    forwarder, buffer = make_forwarder(transport)

    assert forwarder.step() > 0.0
    assert len(buffer) == 3


def test_worker_drains_buffer_in_background_and_stops() -> None:
    transport = FakeTransport()
    forwarder, buffer = make_forwarder(transport, messages=0, idle_interval_s=0.05)
    worker = ForwardWorker(forwarder)
    worker.start()
    try:
        buffer.enqueue("t", "live")
        worker.wake()
        deadline = time.monotonic() + 3.0
        while len(buffer) and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
        worker.join(timeout=3.0)

    assert not worker.is_alive()
    assert len(buffer) == 0
    assert transport.published == [("t", "live")]
