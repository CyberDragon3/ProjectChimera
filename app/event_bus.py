"""Async pub/sub plumbing shared by every tier.

Design:
  * StimulusBus — named asyncio.Queues for ommatidia/pressure/cursor frames.
    Producers (Tier 2) push; consumers (Tier 3) pop.
  * InterruptBus — single queue for reflex fires (Tier 3 → main loop).
  * ExecutiveBus — single queue for LLM events (Tier 1 → UI).
  * PolicyStore — mutable BioPolicy guarded by a lock, with an async
    change-notification channel so Tier 3 can re-read on updates.
  * Snapshot — a lock-free latest-value cache the dashboard reads at ws_hz.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

from .contracts import (
    BioPolicy,
    CursorSample,
    ExecutiveEvent,
    InterruptEvent,
    OmmatidiaFrame,
    PressureSample,
)


def now_ns() -> int:
    return time.perf_counter_ns()


class StimulusBus:
    """Three bounded queues. Bounded = drop-oldest on overflow (reflex cares
    about *recent* stimulus, not a complete history)."""

    def __init__(self, maxsize: int = 16) -> None:
        self.ommatidia: asyncio.Queue[OmmatidiaFrame] = asyncio.Queue(maxsize)
        self.pressure: asyncio.Queue[PressureSample] = asyncio.Queue(maxsize)
        self.cursor: asyncio.Queue[CursorSample] = asyncio.Queue(maxsize)

    async def put_ommatidia(self, f: OmmatidiaFrame) -> None:
        await _put_drop_oldest(self.ommatidia, f)

    async def put_pressure(self, s: PressureSample) -> None:
        await _put_drop_oldest(self.pressure, s)

    async def put_cursor(self, s: CursorSample) -> None:
        await _put_drop_oldest(self.cursor, s)


async def _put_drop_oldest(q: asyncio.Queue, item: Any) -> None:
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    await q.put(item)


class InterruptBus:
    """Fan-in queue for reflex fires + a fan-out list of subscriber queues
    so the dashboard / command bar can mirror events without draining the
    action handler's queue."""

    def __init__(self) -> None:
        self.main: asyncio.Queue[InterruptEvent] = asyncio.Queue(maxsize=256)
        self._subs: list[asyncio.Queue[InterruptEvent]] = []

    def subscribe(self, maxsize: int = 64) -> asyncio.Queue[InterruptEvent]:
        q: asyncio.Queue[InterruptEvent] = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    async def publish(self, ev: InterruptEvent) -> None:
        await self.main.put(ev)
        for q in self._subs:
            await _put_drop_oldest(q, ev)


class ExecutiveBus:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue[ExecutiveEvent]] = []

    def subscribe(self, maxsize: int = 64) -> asyncio.Queue[ExecutiveEvent]:
        q: asyncio.Queue[ExecutiveEvent] = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    async def publish(self, ev: ExecutiveEvent) -> None:
        for q in self._subs:
            await _put_drop_oldest(q, ev)


class PolicyStore:
    """Mutable BioPolicy + async change notifications."""

    def __init__(self, initial: BioPolicy) -> None:
        self._policy = initial
        self._lock = asyncio.Lock()
        self._version = 0
        self._changed = asyncio.Event()

    def get(self) -> BioPolicy:
        # Read is lock-free; dataclasses are simple value holders. Callers
        # that need a coherent multi-field read should .copy() the fields.
        return self._policy

    async def set(self, policy: BioPolicy) -> None:
        async with self._lock:
            self._policy = policy
            self._version += 1
            self._changed.set()
            self._changed.clear()

    @property
    def version(self) -> int:
        return self._version

    async def wait_for_change(self) -> BioPolicy:
        await self._changed.wait()
        return self._policy


@dataclass
class Snapshot:
    """Latest-value cache for the UI broadcast loop. Not a queue — producers
    just overwrite. Small history deques for spike rasters."""
    policy: Optional[BioPolicy] = None
    ommatidia: Optional[OmmatidiaFrame] = None
    pressure: Optional[PressureSample] = None
    cursor: Optional[CursorSample] = None
    sugar_concentration: float = 0.0           # cursor's value on attractor gradient
    fly_spikes: Deque[int] = field(default_factory=lambda: deque(maxlen=300))
    worm_spikes: Deque[int] = field(default_factory=lambda: deque(maxlen=300))
    mouse_spikes: Deque[int] = field(default_factory=lambda: deque(maxlen=300))
    recent_interrupts: Deque[InterruptEvent] = field(default_factory=lambda: deque(maxlen=32))
    recent_executive: Deque[ExecutiveEvent] = field(default_factory=lambda: deque(maxlen=32))
