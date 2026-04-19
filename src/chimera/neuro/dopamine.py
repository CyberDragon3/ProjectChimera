"""Dopamine reward modulator — scalar trace only.

Rewards the Worm when a throttle leaves the user's foreground app smooth,
punishes it when the throttle is followed by a foreground spike inside the
window. Emits ``neuro.dopamine`` events every ``emit_interval_s``.

No neural math: level ∈ [0, 1] with exponential decay + per-outcome deltas.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import structlog

from chimera.bus import Bus, Event

log = structlog.get_logger(__name__)

Outcome = Literal["hit", "miss"]


@dataclass(frozen=True)
class DopamineConfig:
    decay_per_sec: float = 0.1
    hit_delta: float = 0.15
    miss_delta: float = 0.25
    hit_window_s: float = 5.0
    emit_interval_s: float = 1.0
    rolling_outcomes: int = 50


@dataclass
class _State:
    level: float = 0.0
    outcomes: deque[Outcome] = field(default_factory=deque)
    last_outcome: Outcome | None = None


class DopamineModulator:
    """Scalar dopamine trace driven by worm throttle outcomes.

    Subscribes to ``reflex.worm.throttle``, ``cpu.spike`` and
    ``cortex.protect_foreground``. A throttle becomes a *hit* if no foreground
    spike arrives within ``hit_window_s``; a *miss* if one does. A throttle
    aimed at the foreground PID is skipped entirely (Mouse should have vetoed).
    """

    def __init__(self, bus: Bus, cfg: DopamineConfig | None = None) -> None:
        self._bus = bus
        self._cfg = cfg or DopamineConfig()
        self._state = _State(outcomes=deque(maxlen=self._cfg.rolling_outcomes))
        # pid -> monotonic ts of the throttle awaiting classification.
        self._pending: dict[int, float] = {}
        self._foreground_pid: int | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ public
    @property
    def level(self) -> float:
        return self._state.level

    @level.setter
    def level(self, value: float) -> None:
        self._state.level = max(0.0, min(1.0, float(value)))

    @property
    def hit_rate(self) -> float:
        total = len(self._state.outcomes)
        if total == 0:
            return 0.5
        hits = sum(1 for o in self._state.outcomes if o == "hit")
        return hits / total

    def record_outcome(self, outcome: Outcome) -> None:
        """Apply an outcome synchronously (test hook + internal helper)."""
        if outcome == "hit":
            self._state.level = min(1.0, self._state.level + self._cfg.hit_delta)
        else:
            self._state.level = max(0.0, self._state.level - self._cfg.miss_delta)
        self._state.outcomes.append(outcome)
        self._state.last_outcome = outcome

    # -------------------------------------------------------------- run loop
    async def run(self, stop: asyncio.Event) -> None:
        throttle_q = self._bus.subscribe("reflex.worm.throttle")
        spike_q = self._bus.subscribe("cpu.spike")
        protect_q = self._bus.subscribe("cortex.protect_foreground")
        log.info("neuro.dopamine.start")
        try:
            await asyncio.gather(
                self._consume_throttle(throttle_q, stop),
                self._consume_spike(spike_q, stop),
                self._consume_protect(protect_q, stop),
                self._sweep_pending(stop),
                self._emit_loop(stop),
            )
        finally:
            self._bus.unsubscribe("reflex.worm.throttle", throttle_q)
            self._bus.unsubscribe("cpu.spike", spike_q)
            self._bus.unsubscribe("cortex.protect_foreground", protect_q)
            log.info("neuro.dopamine.stop")

    # ------------------------------------------------------------ consumers
    async def _consume_throttle(
        self, q: asyncio.Queue[Event], stop: asyncio.Event
    ) -> None:
        while not stop.is_set():
            event = await _next_event(q)
            if event is None:
                continue
            try:
                pid = int(event.payload.get("pid", -1))
            except (TypeError, ValueError):
                continue
            # Skip: we don't score throttles of the foreground itself.
            if self._foreground_pid is not None and pid == self._foreground_pid:
                continue
            async with self._lock:
                self._pending[pid] = time.monotonic()

    async def _consume_spike(
        self, q: asyncio.Queue[Event], stop: asyncio.Event
    ) -> None:
        while not stop.is_set():
            event = await _next_event(q)
            if event is None:
                continue
            try:
                pid = int(event.payload.get("pid", -1))
            except (TypeError, ValueError):
                continue
            if self._foreground_pid is None or pid != self._foreground_pid:
                continue
            # Foreground spiked — any pending throttle within the window is a miss.
            now = time.monotonic()
            to_resolve: list[int] = []
            async with self._lock:
                for throttle_pid, ts in list(self._pending.items()):
                    if now - ts <= self._cfg.hit_window_s:
                        to_resolve.append(throttle_pid)
                for tp in to_resolve:
                    self._pending.pop(tp, None)
            for _ in to_resolve:
                self.record_outcome("miss")

    async def _consume_protect(
        self, q: asyncio.Queue[Event], stop: asyncio.Event
    ) -> None:
        while not stop.is_set():
            event = await _next_event(q)
            if event is None:
                continue
            fg = event.payload.get("foreground_pid")
            try:
                self._foreground_pid = int(fg) if fg is not None else None
            except (TypeError, ValueError):
                self._foreground_pid = None

    # ----------------------------------------------------------- sweepers
    async def _sweep_pending(self, stop: asyncio.Event) -> None:
        """Promote pending throttles to hits once they outlive the window."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            if stop.is_set():
                return
            now = time.monotonic()
            matured: list[int] = []
            async with self._lock:
                for pid, ts in list(self._pending.items()):
                    if now - ts >= self._cfg.hit_window_s:
                        matured.append(pid)
                for pid in matured:
                    self._pending.pop(pid, None)
            for _ in matured:
                self.record_outcome("hit")

    async def _emit_loop(self, stop: asyncio.Event) -> None:
        last = time.monotonic()
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._cfg.emit_interval_s)
            if stop.is_set():
                return
            now = time.monotonic()
            dt = max(0.0, now - last)
            last = now
            # Apply decay.
            decay = 1.0 - self._cfg.decay_per_sec * dt
            if decay < 0.0:
                decay = 0.0
            self._state.level = max(0.0, min(1.0, self._state.level * decay))
            payload = {
                "level": self._state.level,
                "hit_rate": self.hit_rate,
                "last_outcome": self._state.last_outcome,
            }
            self._bus.publish(Event(topic="neuro.dopamine", payload=payload, ts=now))


async def _next_event(q: asyncio.Queue[Event]) -> Event | None:
    try:
        return await asyncio.wait_for(q.get(), timeout=0.2)
    except TimeoutError:
        return None
