"""Mouse reflex — semantic context filter.

Subscribes to ``window.foreground`` events and republishes them enriched
with an ``intentional`` flag based on a configurable creator-app list.
Acts as the gatekeeper that lets the Worm skip friendly-fire throttling.
"""

from __future__ import annotations

import time

import structlog

from chimera.bus import Bus, Event

log = structlog.get_logger(__name__)

# A small default set of apps that typically produce high, intentional CPU load.
# Users can extend via config in a future iteration.
DEFAULT_CREATOR_APPS: frozenset[str] = frozenset(
    {
        "blender.exe",
        "premiere.exe",
        "adobe premiere pro.exe",
        "aftereffects.exe",
        "davinci resolve.exe",
        "unreal.exe",
        "unrealeditor.exe",
        "unity.exe",
        "ffmpeg.exe",
        "handbrake.exe",
        "code.exe",
        "cursor.exe",
        "pycharm64.exe",
        "devenv.exe",
        "obs64.exe",
        "obs32.exe",
    }
)


class MouseReflex:
    def __init__(self, bus: Bus, creator_apps: frozenset[str] = DEFAULT_CREATOR_APPS) -> None:
        self._bus = bus
        self._creators = frozenset(a.lower() for a in creator_apps)

    async def run(self) -> None:
        q = self._bus.subscribe("window.foreground")
        log.info("reflex.mouse.start", creator_apps=len(self._creators))
        try:
            while True:
                event = await q.get()
                exe = str(event.payload.get("exe", "")).lower()
                intentional = exe in self._creators
                self._bus.publish(
                    Event(
                        topic="context.active_window",
                        payload={
                            **event.payload,
                            "intentional": intentional,
                        },
                        ts=time.monotonic(),
                    )
                )
                log.info(
                    "reflex.mouse.classified",
                    exe=exe,
                    intentional=intentional,
                )
        finally:
            self._bus.unsubscribe("window.foreground", q)
