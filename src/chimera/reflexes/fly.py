"""Fly reflex — translates raw idle events into arousal states.

``idle.enter`` → ``arousal.away``. ``idle.exit`` → ``arousal.present``.
Pure event re-shaping; no OS calls, no safety gate needed.
"""

from __future__ import annotations

import time

import structlog

from chimera.bus import Bus, Event

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
        finally:
            self._bus.unsubscribe("idle", q)
