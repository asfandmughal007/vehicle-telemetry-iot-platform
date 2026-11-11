"""Opaque pagination cursors.

A cursor only carries the sort key (``ts``) of the last item of the previous page. The
vehicle comes from the URL, so a cursor can never be used to read another vehicle's data.
The cursor is *opaque* to clients (base64 JSON) so its content can change without
breaking them.
"""

from __future__ import annotations

import base64
import binascii
import json

from telemetry_api.errors import InvalidQueryError


def encode_cursor(ts: int) -> str:
    raw = json.dumps({"ts": ts}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        ts = data["ts"]
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidQueryError("invalid cursor") from exc
    if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
        raise InvalidQueryError("invalid cursor")
    return ts
