"""Mouse reflex — semantic context filter + cortex veto publisher.

Keeps the original ``context.active_window`` enrichment (creator-apps → intentional).
Adds ``cortex.protect_foreground`` veto when a CPU spike is sourced by the
currently foregrounded PID — the BMTKCortex instructs the Worm to stand down
so the user's active work isn't throttled (see design §5.2).
"""

from __future__ import annotations

import asyncio
import time

import structlog

from chimera.bus import Bus, Event

log = structlog.get_logger(__name__)

DEFAULT_CREATOR_APPS: frozenset[str] = frozenset(
    {
        "blender.exe", "premiere.exe", "adobe premiere pro.exe", "aftereffects.exe",
        "davinci resolve.exe", "unreal.exe", "unrealeditor.exe", "unity.exe",
        "ffmpeg.exe", "handbrake.exe", "code.exe", "cursor.exe",
        "pycharm64.exe", "devenv.exe", "obs64.exe", "obs32.exe",
    }
)


class MouseReflex:
    def __init__(self, bus: Bus, creator_apps: frozenset[str] = DEFAULT_CREATOR_APPS) -> None:
        self._bus = bus
        self._creators = frozenset(a.lower() for a in creator_apps)
        self._foreground_pid: int | None = None
        self._foreground_exe: str | None = None

    def _publish_protect(self, on: bool) -> None:
        self._bus.publish(
            Event(
                topic="cortex.protect_foreground",
                payload={"on": on, "foreground_pid": self._foreground_pid},
                ts=time.monotonic(),
            )
        )

    async def _handle_window(self, event: Event) -> None:
        try:
            pid_raw = event.payload.get("pid")
            self._foreground_pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            self._foreground_pid = None
        exe = str(event.payload.get("exe", "")).lower()
        self._foreground_exe = exe or None
        intentional = exe in self._creators
        self._bus.publish(
            Event(
                topic="context.active_window",
                payload={**event.payload, "intentional": intentional},
                ts=time.monotonic(),
            )
        )
        self._publish_protect(False)
        log.info(
            "reflex.mouse.classified",
            exe=exe, pid=self._foreground_pid, intentional=intentional,
        )

    async def _handle_spike(self, event: Event) -> None:
        try:
            spike_pid = int(event.payload.get("pid", -1))
        except (TypeError, ValueError):
            return
        if self._foreground_pid is not None and spike_pid == self._foreground_pid:
            self._publish_protect(True)
            log.info(
                "reflex.mouse.protect_on",
                pid=spike_pid, exe=event.payload.get("exe"),
            )

    async def run(self) -> None:
        win_q = self._bus.subscribe("window.foreground")
        spike_q = self._bus.subscribe("cpu.spike")
        log.info("reflex.mouse.start", creator_apps=len(self._creators))
        win_task: asyncio.Task[Event] = asyncio.create_task(win_q.get())
        spike_task: asyncio.Task[Event] = asyncio.create_task(spike_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {win_task, spike_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        event = t.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("reflex.mouse.recv_failed", error=str(e))
                        event = None
                    if t is win_task:
                        win_task = asyncio.create_task(win_q.get())
                        if event is not None:
                            await self._handle_window(event)
                    elif t is spike_task:
                        spike_task = asyncio.create_task(spike_q.get())
                        if event is not None:
                            await self._handle_spike(event)
        finally:
            for pending in (win_task, spike_task):
                if not pending.done():
                    pending.cancel()
            self._bus.unsubscribe("window.foreground", win_q)
            self._bus.unsubscribe("cpu.spike", spike_q)
