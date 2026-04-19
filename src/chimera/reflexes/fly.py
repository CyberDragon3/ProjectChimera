"""Fly reflex — translates raw idle events into arousal states.

``idle.enter`` → ``arousal.away``. ``idle.exit`` → ``arousal.present``.
Pure event re-shaping; no OS calls, no safety gate needed.

Two classes are exposed:

- :class:`FlyReflex` — legacy pure event re-shape.
- :class:`FlyNeuroReflex` — LIF-neuron-driven arousal.away, fed by accumulated
  idle-seconds plus Gaussian membrane noise (jittery fire time).

The daemon picks between them based on ``settings.neuro.enabled``.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import numpy as np
import structlog

from chimera.bus import Bus, Event
from chimera.neuro.glif import LIFNeuron

if TYPE_CHECKING:
    from chimera.config import NeuroCfg

log = structlog.get_logger(__name__)


class FlyReflex:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus

    async def run(self) -> None:
        q = self._bus.subscribe("idle")
        log.info("reflex.fly.start")
        try:
            while True:
                event = await q.get()
                try:
                    if event.topic == "idle.enter":
                        out = "arousal.away"
                    elif event.topic == "idle.exit":
                        out = "arousal.present"
                    else:
                        continue
                    self._bus.publish(
                        Event(topic=out, payload=dict(event.payload), ts=time.monotonic())
                    )
                    log.info("reflex.fly.arousal", state=out.rsplit(".", 1)[-1])
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("reflex.fly.handler_failed", error=str(e))
        finally:
            self._bus.unsubscribe("idle", q)


class FlyNeuroReflex:
    """LIF-neuron-driven Fly. Idle-seconds drive a single neuron; a Gaussian
    noise current added externally yields natural jitter in the fire time.

    Behavior:
    - ``idle.enter``: begin accumulating ``_idle_seconds`` and driving the
      neuron each tick. Arms the one-shot ``arousal.away`` latch.
    - ``idle.exit``: reset the neuron and the idle counter, publish
      ``arousal.present`` (forwarding the sensor payload).
    - On first spike in an idle session: publish ``arousal.away`` and
      ``neuro.fly.spike {v, noise}``. Subsequent spikes in the same session
      are suppressed to prevent arousal chatter.
    """

    def __init__(
        self,
        bus: Bus,
        *,
        neuro_cfg: NeuroCfg,
        rng: np.random.Generator | None = None,
    ) -> None:
        self._bus = bus
        self._cfg = neuro_cfg
        tick_hz = max(1, int(neuro_cfg.tick_hz))
        self._dt_s: float = 1.0 / tick_hz
        dt_ms = 1000.0 / tick_hz
        self._rng: np.random.Generator = (
            rng if rng is not None else np.random.default_rng()
        )
        self._noise_sigma_mv: float = float(neuro_cfg.noise_sigma_mv)
        # Noise is injected externally so we can report it on the spike
        # payload. The neuron itself runs with noise_sigma_mv=0.
        self._neuron = LIFNeuron(
            tau_m_ms=neuro_cfg.tau_m_ms,
            v_rest_mv=neuro_cfg.v_rest_mv,
            v_reset_mv=neuro_cfg.v_reset_mv,
            v_thresh_mv=neuro_cfg.v_thresh_mv,
            refractory_ms=neuro_cfg.refractory_ms,
            dt_ms=dt_ms,
            noise_sigma_mv=0.0,
        )
        self._input_gain: float = float(neuro_cfg.fly_input_gain)
        self._in_idle: bool = False
        self._has_fired: bool = False
        self._idle_seconds: float = 0.0

    def _handle_event(self, event: Event) -> None:
        if event.topic == "idle.enter":
            seconds = float(event.payload.get("seconds", 0.0) or 0.0)
            self._in_idle = True
            self._has_fired = False
            self._idle_seconds = seconds
            self._neuron.reset()
            log.info("reflex.fly.neuro.idle_enter", seconds=seconds)
        elif event.topic == "idle.exit":
            self._in_idle = False
            self._has_fired = False
            self._idle_seconds = 0.0
            self._neuron.reset()
            payload = dict(event.payload)
            self._bus.publish(
                Event(topic="arousal.present", payload=payload, ts=time.monotonic())
            )
            log.info("reflex.fly.neuro.arousal", state="present")

    def _tick(self) -> None:
        drive = self._idle_seconds * self._input_gain if self._in_idle else 0.0
        noise = (
            float(self._rng.normal(0.0, self._noise_sigma_mv))
            if self._noise_sigma_mv > 0.0
            else 0.0
        )
        current = drive + noise
        spiked = self._neuron.step(current)
        if self._in_idle:
            self._idle_seconds += self._dt_s
        if spiked and self._in_idle and not self._has_fired:
            self._has_fired = True
            now = time.monotonic()
            v = float(self._neuron.v)
            self._bus.publish(
                Event(
                    topic="neuro.fly.spike",
                    payload={"v": v, "noise": noise},
                    ts=now,
                )
            )
            self._bus.publish(
                Event(
                    topic="arousal.away",
                    payload={"seconds": float(self._idle_seconds)},
                    ts=now,
                )
            )
            log.info(
                "reflex.fly.neuro.arousal",
                state="away",
                v=v,
                noise=noise,
                idle_seconds=self._idle_seconds,
            )

    async def run(self) -> None:
        q = self._bus.subscribe("idle")
        log.info(
            "reflex.fly.neuro.start",
            tick_hz=self._cfg.tick_hz,
            fly_input_gain=self._input_gain,
            noise_sigma_mv=self._noise_sigma_mv,
        )
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=self._dt_s)
                except TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                else:
                    try:
                        self._handle_event(event)
                    except Exception as e:
                        log.exception("reflex.fly.neuro.handler_failed", error=str(e))
                try:
                    self._tick()
                except Exception as e:
                    log.exception("reflex.fly.neuro.tick_failed", error=str(e))
        finally:
            self._bus.unsubscribe("idle", q)
