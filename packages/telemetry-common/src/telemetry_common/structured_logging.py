"""Structured (JSON) logging shared by all services.

One JSON object per line on stderr. Context is attached through the standard
``extra=`` argument, so application code stays on the stdlib ``logging`` API::

    logger.info("dtc_set", extra={"vehicle_id": "VH-0001", "dtc": "P0217"})

The format is queryable as-is in CloudWatch Logs Insights
(``filter level = "ERROR" | stats count() by service``).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# Attributes every LogRecord has; anything else was supplied via ``extra=``.
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str: never let an unserialisable context value break logging.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    service: str,
    level: str = "INFO",
    *,
    json_output: bool = True,
    stream: TextIO | None = None,
) -> None:
    """Configure the root logger. Call once at process start.

    Args:
        service: Service name added to every record (e.g. ``"vehicle-simulator"``).
        level: Log level name (``DEBUG``, ``INFO``, ...).
        json_output: ``False`` switches to a human-readable format for local debugging.
        stream: Target stream, defaults to stderr (stdout stays free for data output).
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
