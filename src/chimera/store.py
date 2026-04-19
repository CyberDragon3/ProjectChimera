"""Ring-buffer telemetry store with slope helpers.

Per-metric ``collections.deque(maxlen=N)`` is an O(1) ring in C. Slope is
computed on demand by copying the deque into a numpy array and running a
first-degree polyfit.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(slots=True, frozen=True)
class Sample:
    t: float
    v: float


class RingBuffer:
    """Time-stamped ring buffer for a single metric."""

    def __init__(self, max_seconds: float, max_points: int = 10_000) -> None:
        self._max_seconds = max_seconds
        self._buf: deque[Sample] = deque(maxlen=max_points)

    def append(self, value: float, ts: float | None = None) -> None:
        now = ts if ts is not None else time.monotonic()
        self._buf.append(Sample(now, value))
        cutoff = now - self._max_seconds
        while self._buf and self._buf[0].t < cutoff:
            self._buf.popleft()

    def __len__(self) -> int:
        return len(self._buf)

    def latest(self) -> Sample | None:
        return self._buf[-1] if self._buf else None

    def window(self, seconds: float) -> list[Sample]:
        now = time.monotonic()
        cutoff = now - seconds
        return [s for s in self._buf if s.t >= cutoff]

    def slope(self, seconds: float) -> float:
        """Linear slope (value-units per second) over the last ``seconds``.

        Returns 0.0 when fewer than two samples exist in the window.
        """
        samples = self.window(seconds)
        if len(samples) < 2:
            return 0.0
        ts = np.fromiter((s.t for s in samples), dtype=np.float64, count=len(samples))
        vs = np.fromiter((s.v for s in samples), dtype=np.float64, count=len(samples))
        # polyfit on a singular matrix (all timestamps identical) raises
        # LinAlgError. With monotonic-clock samples this is rare but possible.
        if float(np.ptp(ts)) == 0.0:
            return 0.0
        try:
            slope, _ = np.polyfit(ts, vs, 1)
        except np.linalg.LinAlgError:
            return 0.0
        return float(slope)
