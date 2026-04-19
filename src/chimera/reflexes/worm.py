"""Worm reflex — CPU pain response with deny-by-default safety gate.

Subscribes to ``cpu.spike`` events and (optionally) a ``window.foreground``
channel for intentional-work context. Calls ``psutil.Process.nice`` to
non-destructively demote offending processes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

import psutil
import structlog

from chimera.bus import Bus, Event
from chimera.safety import ProtectedSpecies

log = structlog.get_logger(__name__)

try:
    _BELOW_NORMAL = psutil.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]
    _IDLE_PRIO = psutil.IDLE_PRIORITY_CLASS  # type: ignore[attr-defined]
except AttributeError:  # non-Windows
    _BELOW_NORMAL = 10
    _IDLE_PRIO = 19


class Throttler(Protocol):
    def demote(self, pid: int, level: int) -> bool: ...


class PsutilThrottler:
    """Lowers process priority. Never kills."""

    def demote(self, pid: int, level: int) -> bool:
        try:
            p = psutil.Process(pid)
            p.nice(level)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.warning("worm.demote_failed", pid=pid, error=str(e))
            return False


class WormReflex:
    """Fast pain reflex. Hard deadline guarded by ``asyncio.wait_for``."""

    def __init__(
        self,
        bus: Bus,
        safety: ProtectedSpecies,
        throttler: Throttler,
        deadline_ms: int = 10,
    ) -> None:
        self._bus = bus
        self._safety = safety
        self._throttler = throttler
        self._deadline = deadline_ms / 1000.0
        self._intentional: dict[str, float] = {}  # exe_name -> expiry ts (monotonic)

    def mark_intentional(self, exe: str, ttl_seconds: float = 10.0) -> None:
        """Called by the Mouse reflex when the user is actively using an app."""
        self._intentional[exe.lower()] = time.monotonic() + ttl_seconds

    def _is_intentional(self, exe: str) -> bool:
        expiry = self._intentional.get(exe.lower())
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            self._intentional.pop(exe.lower(), None)
            return False
        return True

    async def _handle(self, event: Event) -> None:
        exe = str(event.payload.get("exe", ""))
        try:
            pid = int(event.payload.get("pid", -1))
            pct = float(event.payload.get("cpu_percent", 0.0))
        except (TypeError, ValueError):
            log.warning("reflex.worm.bad_payload", payload=event.payload)
            return

        if not self._safety.gate(exe, action="throttle", pid=pid):
            return
        if self._is_intentional(exe):
            log.info("worm.skip.intentional", pid=pid, exe=exe, cpu_percent=pct)
            return

        ok = self._throttler.demote(pid, _BELOW_NORMAL)
        self._bus.publish(
            Event(
                topic="reflex.worm.throttle",
                payload={"pid": pid, "exe": exe, "cpu_percent": pct, "ok": ok},
                ts=time.monotonic(),
            )
        )
        log.info(
            "reflex.worm.throttle",
            pid=pid,
            exe=exe,
            cpu_percent=pct,
            ok=ok,
        )

    async def run(self) -> None:
        q = self._bus.subscribe("cpu.spike")
        win_q = self._bus.subscribe("window.foreground")
        log.info("reflex.worm.start", deadline_ms=int(self._deadline * 1000))
        # Hoist getter tasks outside the loop so we don't leak a fresh pair per
        # iteration (FIRST_COMPLETED leaves the pending getter dangling, which
        # over time accumulates phantom waiters and loses events).
        spike_task: asyncio.Task[Event] = asyncio.create_task(q.get())
        win_task: asyncio.Task[Event] = asyncio.create_task(win_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {spike_task, win_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        event = t.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("reflex.worm.recv_failed", error=str(e))
                        event = None
                    if t is spike_task:
                        spike_task = asyncio.create_task(q.get())
                    elif t is win_task:
                        win_task = asyncio.create_task(win_q.get())
                    if event is None:
                        continue
                    if event.topic.startswith("window"):
                        exe = str(event.payload.get("exe", ""))
                        if exe:
                            self.mark_intentional(exe)
                        continue
                    try:
                        await asyncio.wait_for(self._handle(event), timeout=self._deadline)
                    except asyncio.TimeoutError:
                        log.warning("reflex.worm.deadline_exceeded", event=event.topic)
                    except Exception as e:
                        log.exception("reflex.worm.handler_failed", error=str(e))
        finally:
            for pending in (spike_task, win_task):
                if not pending.done():
                    pending.cancel()
            self._bus.unsubscribe("cpu.spike", q)
            self._bus.unsubscribe("window.foreground", win_q)
