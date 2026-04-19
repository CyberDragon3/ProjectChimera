"""Zebrafish governor — thermal metabolism slope analyzer.

Reads from a shared RingBuffer populated by ThermalSensor. Every tick, it
computes the slope over the last N seconds; if the rate of rise exceeds a
threshold it publishes ``thermal.rising`` events with a severity tier.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from chimera.bus import Bus, Event
from chimera.store import RingBuffer

log = structlog.get_logger(__name__)


class ZebrafishReflex:
    def __init__(
        self,
        bus: Bus,
        buffer: RingBuffer,
        slope_c_per_min_threshold: float,
        window_seconds: float = 60.0,
        interval_ms: int = 5000,
    ) -> None:
        self._bus = bus
        self._buf = buffer
        self._threshold_per_sec = slope_c_per_min_threshold / 60.0
        self._window = window_seconds
        self._interval = interval_ms / 1000.0

    async def run(self) -> None:
        log.info(
            "reflex.zebrafish.start",
            threshold_c_per_min=self._threshold_per_sec * 60,
            window_s=self._window,
        )
        while True:
            try:
                slope = self._buf.slope(self._window)
                if slope >= self._threshold_per_sec:
                    severity = "critical" if slope >= 2 * self._threshold_per_sec else "warn"
                    self._bus.publish(
                        Event(
                            topic="thermal.rising",
                            payload={
                                "slope_c_per_min": slope * 60,
                                "severity": severity,
                                "window_s": self._window,
                            },
                            ts=time.monotonic(),
                        )
                    )
                    log.info(
                        "reflex.zebrafish.rising",
                        slope_c_per_min=slope * 60,
                        severity=severity,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("reflex.zebrafish.iteration_failed", error=str(e))
            await asyncio.sleep(self._interval)
