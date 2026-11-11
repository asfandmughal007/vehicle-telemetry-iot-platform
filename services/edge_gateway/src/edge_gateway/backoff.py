"""Exponential backoff with jitter for retries."""

from __future__ import annotations

import random


class ExponentialBackoff:
    """``base * 2**attempt`` capped at ``maximum``, randomised within [50 %, 100 %].

    The jitter prevents a fleet of vehicles that lost connectivity together (e.g. a
    tunnel, a broker restart) from reconnecting in lock-step and overloading the broker.
    """

    def __init__(self, base_s: float, maximum_s: float, rng: random.Random | None = None) -> None:
        if base_s <= 0 or maximum_s < base_s:
            raise ValueError("require 0 < base_s <= maximum_s")
        self._base_s = base_s
        self._maximum_s = maximum_s
        self._rng = rng if rng is not None else random.Random()
        self._attempt = 0

    def next_delay(self) -> float:
        ceiling = min(self._maximum_s, self._base_s * (2**self._attempt))
        self._attempt = min(self._attempt + 1, 32)  # keep 2**attempt bounded
        return float(ceiling * self._rng.uniform(0.5, 1.0))

    def reset(self) -> None:
        self._attempt = 0
