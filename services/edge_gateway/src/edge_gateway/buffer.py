"""Persistent FIFO queue (SQLite) for store-and-forward.

Every outgoing message is written here first and removed only after the broker has
acknowledged it (PUBACK). That gives at-least-once delivery that survives network
outages *and* gateway restarts or power loss. SQLite is in the Python standard
library, transactional, and a well-understood choice on embedded Linux.

When the buffer is full the **oldest** messages are dropped: for live monitoring,
recent data is worth more than old data, and a bounded buffer protects the flash
storage of the device.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    topic   TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class BufferedMessage:
    id: int
    topic: str
    payload: str


class SqliteBuffer:
    """Thread-safe: the CAN thread enqueues while the forwarder thread dequeues."""

    def __init__(self, path: str | Path, max_messages: int) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        self._max_messages = max_messages
        self.dropped_total = 0
        self._lock = threading.Lock()

        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def enqueue(self, topic: str, payload: str) -> int:
        """Append a message. Returns how many old messages were dropped to make room."""
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO queue (topic, payload) VALUES (?, ?)", (topic, payload))
            excess = self._count() - self._max_messages
            if excess <= 0:
                return 0
            self._conn.execute(
                "DELETE FROM queue WHERE id IN (SELECT id FROM queue ORDER BY id LIMIT ?)",
                (excess,),
            )
        self.dropped_total += excess
        logger.warning("buffer_overflow", extra={"dropped": excess, "capacity": self._max_messages})
        return excess

    def peek(self, limit: int) -> list[BufferedMessage]:
        """Return the oldest ``limit`` messages without removing them."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, topic, payload FROM queue ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [BufferedMessage(*row) for row in rows]

    def delete(self, ids: Iterable[int]) -> None:
        with self._lock, self._conn:
            self._conn.executemany("DELETE FROM queue WHERE id = ?", [(i,) for i in ids])

    def __len__(self) -> int:
        with self._lock:
            return self._count()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM queue").fetchone()
        return int(row[0])
