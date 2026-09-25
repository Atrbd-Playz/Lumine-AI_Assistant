"""Small latency recorder for the voice runtime.

The recorder emits the same stdout event contract as the rest of the worker.
It deliberately stores only stage names, durations, and provider metadata—never
conversation text or credentials.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class LatencyTracker:
    def __init__(self, emit: Callable[..., None]) -> None:
        self._emit = emit
        self._started = time.monotonic()
        self._stage_times: dict[str, float] = {}

    def mark(self, stage: str, **details: object) -> float:
        now = time.monotonic()
        elapsed_ms = round((now - self._started) * 1000)
        self._stage_times[stage] = now
        self._emit("latency", stage=stage, elapsed_ms=elapsed_ms, **details)
        return float(elapsed_ms)

    def since(self, stage: str) -> float | None:
        started = self._stage_times.get(stage)
        if started is None:
            return None
        return round((time.monotonic() - started) * 1000, 3)
