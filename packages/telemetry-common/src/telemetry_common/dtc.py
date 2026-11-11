"""Packing of diagnostic trouble codes (DTCs) for transport on CAN.

A DTC such as ``P0217`` is transmitted as two integers:

* category: index of the system letter (P=0 powertrain, C=1 chassis, B=2 body, U=3 network)
* number:   the four hex digits as a 16-bit value (``0x0217``)
"""

from __future__ import annotations

import re

from telemetry_common.schema import DTC_PATTERN

DTC_CATEGORIES: tuple[str, ...] = ("P", "C", "B", "U")
MAX_DTC_NUMBER = 0xFFFF

_DTC_RE = re.compile(DTC_PATTERN)


class InvalidDtcError(ValueError):
    """Raised when a DTC string or its packed representation is invalid."""


def encode_dtc(code: str) -> tuple[int, int]:
    """Convert ``"P0217"`` into ``(category, number)`` = ``(0, 0x0217)``."""
    if not _DTC_RE.fullmatch(code):
        raise InvalidDtcError(f"not a valid DTC: {code!r}")
    return DTC_CATEGORIES.index(code[0]), int(code[1:], 16)


def decode_dtc(category: int, number: int) -> str:
    """Convert ``(category, number)`` back into the textual DTC."""
    if not 0 <= category < len(DTC_CATEGORIES):
        raise InvalidDtcError(f"DTC category out of range: {category}")
    if not 0 <= number <= MAX_DTC_NUMBER:
        raise InvalidDtcError(f"DTC number out of range: {number}")
    return f"{DTC_CATEGORIES[category]}{number:04X}"
