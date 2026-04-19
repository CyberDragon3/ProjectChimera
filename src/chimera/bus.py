"""In-process async pub/sub event bus.

Single-process, zero-dep fan-out using asyncio.Queue. Sub-µs publish path;
slow subscribers never block publishers because each subscriber owns its own
bounded queue and we drop oldest on overflow.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class Event:
    """An immutable event published on the bus."""

    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


class Bus:
    """Topic-based pub/sub. Subscribers get one Queue per call to subscribe().

    Topics are hierarchical strings (e.g. ``cpu.spike``, ``thermal.rising``).
    A subscriber to ``cpu`` receives all ``cpu.*`` events.
    """

    def __init__(self, queue_maxsize: int = 256) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event]]] = {}
        self._lock = asyncio.Lock()
        self._queue_maxsize = queue_maxsize
        self._dropped = 0

    def subscribe(self, topic_prefix: str) -> asyncio.Queue[Event]:
        """Return a new Queue that receives events whose topic starts with prefix."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subs.setdefault(topic_prefix, set()).add(queue)
        return queue

    def unsubscribe(self, topic_prefix: str, queue: asyncio.Queue[Event]) -> None:
        subs = self._subs.get(topic_prefix)
        if subs is not None:
            subs.discard(queue)

    def publish(self, event: Event) -> None:
        """Non-blocking publish. Drops oldest on overflow (telemetry over correctness)."""
        # Snapshot to defend against concurrent subscribe/unsubscribe mutation.
        for prefix, queues in tuple(self._subs.items()):
            matched = (
                prefix == ""
                or event.topic == prefix
                or event.topic.startswith(prefix + ".")
            )
            if not matched:
                continue
            for q in tuple(queues):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    self._dropped += 1
                    # True drop-oldest: evict head and retry once.
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    async def stream(self, topic_prefix: str) -> AsyncIterator[Event]:
        """Async iterator over a subscription. Unsubscribes on cancel."""
        q = self.subscribe(topic_prefix)
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(topic_prefix, q)

    @property
    def dropped(self) -> int:
        return self._dropped
