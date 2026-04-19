"""Zebrafish governor — thermal metabolism slope + hard-floor critical veto.

Two independent signals:
- ``thermal.rising`` — slope over a 60 s window (existing).
- ``thermal.critical`` — hard-floor absolute threshold with hysteresis (new).
  Consumed by Worm as the supreme-veto override (see design §4.2).
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
        critical_c: float = 95.0,
        critical_clear_c: float = 90.0,
        critical_samples: int = 2,
        max_hold_seconds: int = 300,
        window_seconds: float = 60.0,
        interval_ms: int = 5000,
    ) -> None:
        self._bus = bus
        self._buf = buffer
        self._threshold_per_sec = slope_c_per_min_threshold / 60.0
        self._critical_c = critical_c
        self._critical_clear_c = critical_clear_c
        self._critical_samples = critical_samples
        self._max_hold = max_hold_seconds
        self._window = window_seconds
        self._interval = interval_ms / 1000.0
        self._critical_on = False
        self._critical_entered_at: float | None = None

    def _eval_critical(self) -> None:
        recent = self._buf.last_n(self._critical_samples)
        if len(recent) < self._critical_samples:
            return
        now = time.monotonic()
        if not self._critical_on and all(c >= self._critical_c for c in recent):
            self._critical_on = True
            self._critical_entered_at = now
            self._bus.publish(
                Event(
                    topic="thermal.critical",
                    payload={"on": True, "celsius": recent[-1]},
                    ts=now,
                )
            )
            log.warning("reflex.zebrafish.critical_entered", celsius=recent[-1])
            return
        if self._critical_on:
            if all(c <= self._critical_clear_c for c in recent):
                self._critical_on = False
                self._critical_entered_at = None
                self._bus.publish(
                    Event(
                        topic="thermal.critical",
                        payload={"on": False, "celsius": recent[-1]},
                        ts=now,
                    )
                )
                log.info("reflex.zebrafish.critical_cleared", celsius=recent[-1])
                return
            if (
                self._critical_entered_at is not None
                and now - self._critical_entered_at > self._max_hold
            ):
                self._critical_on = False
                self._critical_entered_at = None
                self._bus.publish(
                    Event(
                        topic="thermal.critical",
                        payload={
                            "on": False,
                            "celsius": recent[-1],
                            "reason": "max_hold_exceeded",
                        },
                        ts=now,
                    )
                )
                log.warning("reflex.zebrafish.critical_suspicious_auto_clear")

    def _eval_slope(self) -> None:
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

    async def run(self) -> None:
        log.info(
            "reflex.zebrafish.start",
            threshold_c_per_min=self._threshold_per_sec * 60,
            critical_c=self._critical_c,
            critical_clear_c=self._critical_clear_c,
        )
        while True:
            try:
                self._eval_slope()
                self._eval_critical()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("reflex.zebrafish.iteration_failed", error=str(e))
            await asyncio.sleep(self._interval)
