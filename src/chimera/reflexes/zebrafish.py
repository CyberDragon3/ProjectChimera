"""Zebrafish governor — thermal metabolism slope + hard-floor critical veto.

Two independent signals:
- ``thermal.rising`` — slope over a 60 s window (existing).
- ``thermal.critical`` — hard-floor absolute threshold with hysteresis (new).
  Consumed by Worm as the supreme-veto override (see design §4.2).

Two classes are exposed:

- :class:`ZebrafishReflex` — legacy slope+hard-floor implementation.
- :class:`ZebrafishNeuroReflex` — LIF-neuron-driven alert with the hard floor
  retained as a belt-and-braces fallback.

The daemon picks between them based on ``settings.neuro.enabled``.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from chimera.bus import Bus, Event
from chimera.neuro.glif import LIFNeuron
from chimera.store import RingBuffer

if TYPE_CHECKING:
    from chimera.config import NeuroCfg

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


class ZebrafishNeuroReflex:
    """LIF-neuron-driven thermal governor with belt-and-braces hard floor.

    A single :class:`LIFNeuron` integrates the thermal slope (°C/s ->
    effective mV drive via ``neuro_cfg.zebrafish_input_gain``) at
    ``neuro_cfg.tick_hz``. A spike re-raises ``thermal.critical``; the
    absolute ``critical_c`` hard floor still triggers independently so a
    slow steady-state cook cannot hide under a quiet neuron.
    """

    def __init__(
        self,
        bus: Bus,
        buffer: RingBuffer,
        *,
        neuro_cfg: NeuroCfg,
        slope_c_per_min_threshold: float = 2.5,
        critical_c: float = 95.0,
        critical_clear_c: float = 90.0,
        critical_samples: int = 2,
        max_hold_seconds: int = 300,
        window_seconds: float = 5.0,
    ) -> None:
        self._bus = bus
        self._buf = buffer
        self._neuro_cfg = neuro_cfg
        self._slope_per_min_threshold = slope_c_per_min_threshold
        self._critical_c = critical_c
        self._critical_clear_c = critical_clear_c
        self._critical_samples = critical_samples
        self._max_hold = max_hold_seconds
        self._window = window_seconds

        tick_hz = max(1, int(neuro_cfg.tick_hz))
        self._tick_hz = tick_hz
        self._dt_s = 1.0 / tick_hz
        self._input_gain = float(neuro_cfg.zebrafish_input_gain)

        # Determinism on the thermal path: no noise.
        self._neuron = LIFNeuron(
            tau_m_ms=float(neuro_cfg.tau_m_ms),
            v_rest_mv=float(neuro_cfg.v_rest_mv),
            v_reset_mv=float(neuro_cfg.v_reset_mv),
            v_thresh_mv=float(neuro_cfg.v_thresh_mv),
            refractory_ms=float(neuro_cfg.refractory_ms),
            dt_ms=1000.0 / tick_hz,
            noise_sigma_mv=0.0,
        )

        self._critical_on = False
        self._critical_entered_at: float | None = None
        # Cooldown: count consecutive ticks where slope <= 0 while held.
        self._clear_samples = 0

    # --- internals ---------------------------------------------------

    def _publish_critical_on(self, celsius: float, cause: str, now: float) -> None:
        self._critical_on = True
        self._critical_entered_at = now
        self._clear_samples = 0
        self._bus.publish(
            Event(
                topic="thermal.critical",
                payload={"on": True, "celsius": celsius, "cause": cause},
                ts=now,
            )
        )
        log.warning(
            "reflex.zebrafish.neuro.critical_entered",
            celsius=celsius,
            cause=cause,
        )

    def _publish_critical_off(
        self, celsius: float, reason: str, now: float
    ) -> None:
        self._critical_on = False
        self._critical_entered_at = None
        self._clear_samples = 0
        self._bus.publish(
            Event(
                topic="thermal.critical",
                payload={"on": False, "celsius": celsius, "reason": reason},
                ts=now,
            )
        )
        log.info(
            "reflex.zebrafish.neuro.critical_cleared",
            celsius=celsius,
            reason=reason,
        )

    def _hard_floor_triggered(self) -> bool:
        recent = self._buf.last_n(self._critical_samples)
        if len(recent) < self._critical_samples:
            return False
        return all(c >= self._critical_c for c in recent)

    def _hard_floor_clear(self) -> bool:
        recent = self._buf.last_n(self._critical_samples)
        if len(recent) < self._critical_samples:
            return False
        return all(c <= self._critical_clear_c for c in recent)

    def _latest_celsius(self) -> float:
        latest = self._buf.latest()
        return float(latest.v) if latest is not None else float("nan")

    def _tick(self) -> None:
        now = time.monotonic()

        slope_c_per_sec = self._buf.slope(self._window)
        current_mv = slope_c_per_sec * self._input_gain
        spiked = self._neuron.step(current_mv)

        if spiked:
            self._bus.publish(
                Event(
                    topic="neuro.zebrafish.spike",
                    payload={
                        "v": self._neuron.v,
                        "current": current_mv,
                    },
                    ts=now,
                )
            )
            log.debug(
                "reflex.zebrafish.neuro.spike",
                v=self._neuron.v,
                current=current_mv,
            )

        # --- entry conditions (either trigger latches critical_on) ---
        if not self._critical_on:
            if spiked:
                self._publish_critical_on(
                    self._latest_celsius(), cause="lif_spike", now=now
                )
                return
            if self._hard_floor_triggered():
                self._publish_critical_on(
                    self._latest_celsius(), cause="hard_floor", now=now
                )
                return
            return

        # --- hold / release ---
        # Hard-floor clear short-circuits the hysteresis regardless of slope.
        if self._hard_floor_clear():
            self._publish_critical_off(
                self._latest_celsius(), reason="hard_floor_clear", now=now
            )
            return

        # Slope cooldown: non-positive slope must persist for N ticks.
        if slope_c_per_sec <= 0.0:
            self._clear_samples += 1
        else:
            self._clear_samples = 0

        if self._clear_samples >= self._critical_samples:
            self._publish_critical_off(
                self._latest_celsius(), reason="slope_cooldown", now=now
            )
            return

        if (
            self._critical_entered_at is not None
            and now - self._critical_entered_at > self._max_hold
        ):
            self._publish_critical_off(
                self._latest_celsius(),
                reason="max_hold_exceeded",
                now=now,
            )
            log.warning("reflex.zebrafish.neuro.critical_suspicious_auto_clear")

    async def run(self) -> None:
        log.info(
            "reflex.zebrafish.neuro.start",
            tick_hz=self._tick_hz,
            input_gain=self._input_gain,
            critical_c=self._critical_c,
            critical_clear_c=self._critical_clear_c,
        )
        while True:
            try:
                self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(
                    "reflex.zebrafish.neuro.iteration_failed", error=str(e)
                )
            await asyncio.sleep(self._dt_s)
