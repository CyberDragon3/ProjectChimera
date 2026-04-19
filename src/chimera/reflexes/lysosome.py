"""Lysosome scavenger — idle-time cleanup reflex.

Three-phase sweep triggered on ``idle.enter`` (the Fly's Deep-Breath state):
  1. Working-set trim — non-destructive, OS re-pages on demand.
  2. System cache flush — admin-only; logs + skips on access-denied.
  3. Opt-in target kill — only exes in [lysosome] targets; safety-gated.

See design §5.5. Added to test_safety_audit's ALLOWED_MODULES in Phase 7
because phase 3 invokes ``proc.kill()`` after passing through ``safety.gate``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterable
from typing import Protocol

import structlog

from chimera.bus import Bus, Event
from chimera.safety import ProtectedSpecies

log = structlog.get_logger(__name__)


class LysosomeBackend(Protocol):
    def trim_working_set(self, pids: Iterable[int]) -> int: ...
    def flush_system_cache(self) -> int | None: ...
    def kill(self, pid: int) -> bool: ...


class NullLysosomeBackend:
    """Non-Windows / test fallback. All phases become no-ops."""

    def trim_working_set(self, pids: Iterable[int]) -> int:
        return 0

    def flush_system_cache(self) -> int | None:
        return None

    def kill(self, pid: int) -> bool:
        return False


class LysosomeReflex:
    """Idle-time scavenger. Runs three phases in sequence per sweep."""

    def __init__(
        self,
        bus: Bus,
        safety: ProtectedSpecies,
        backend: LysosomeBackend,
        *,
        enabled: bool = True,
        sweep_interval_seconds: int = 600,
        targets: tuple[str, ...] = (),
        pid_scanner=None,
    ) -> None:
        self._bus = bus
        self._safety = safety
        self._backend = backend
        self._enabled = enabled
        self._interval = sweep_interval_seconds
        self._targets = frozenset(t.lower() for t in targets)
        self._pid_scanner = pid_scanner or _default_pid_scanner
        self._last_sweep: float = 0.0
        self._abort = asyncio.Event()
        self.sweep_count: int = 0

    def _eligible_for_trim(self, procs: list[tuple[int, str]]) -> list[int]:
        return [
            pid for pid, exe in procs
            if not self._safety.is_protected(exe, pid=pid)
        ]

    def _target_hits(self, procs: list[tuple[int, str]]) -> list[tuple[int, str]]:
        return [
            (pid, exe) for pid, exe in procs
            if exe.lower() in self._targets
        ]

    async def _sweep(self) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        if self._last_sweep and (now - self._last_sweep) < self._interval:
            log.info("reflex.lysosome.rate_limited", elapsed=now - self._last_sweep)
            return
        self._last_sweep = now
        self._abort.clear()
        self.sweep_count += 1
        procs = list(self._pid_scanner())

        # Phase 1 — working-set trim
        if self._abort.is_set():
            return
        trim_pids = self._eligible_for_trim(procs)
        count = await asyncio.to_thread(self._backend.trim_working_set, trim_pids)
        self._bus.publish(Event(
            topic="lysosome.sweep",
            payload={"phase": "workingset", "count": count, "bytes_freed": None},
            ts=time.monotonic(),
        ))
        log.info("reflex.lysosome.workingset_trimmed", count=count)

        # Phase 2 — system cache flush
        if self._abort.is_set():
            return
        bytes_freed = await asyncio.to_thread(self._backend.flush_system_cache)
        self._bus.publish(Event(
            topic="lysosome.sweep",
            payload={
                "phase": "cachetrim",
                "count": 1 if bytes_freed is not None else 0,
                "bytes_freed": bytes_freed,
            },
            ts=time.monotonic(),
        ))
        log.info("reflex.lysosome.cache_flushed", bytes_freed=bytes_freed)

        # Phase 3 — opt-in target kill
        killed = 0
        for pid, exe in self._target_hits(procs):
            if self._abort.is_set():
                break
            if not self._safety.gate(exe, action="kill", pid=pid):
                continue
            if await asyncio.to_thread(self._backend.kill, pid):
                killed += 1
                log.info("reflex.lysosome.target_killed", pid=pid, exe=exe)
        self._bus.publish(Event(
            topic="lysosome.sweep",
            payload={"phase": "targetkill", "count": killed, "bytes_freed": None},
            ts=time.monotonic(),
        ))

    async def run(self) -> None:
        deep_q = self._bus.subscribe("idle.enter")
        exit_q = self._bus.subscribe("idle.exit")
        log.info(
            "reflex.lysosome.start",
            enabled=self._enabled,
            interval_s=self._interval,
            targets=len(self._targets),
        )
        enter_task: asyncio.Task[Event] = asyncio.create_task(deep_q.get())
        exit_task: asyncio.Task[Event] = asyncio.create_task(exit_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {enter_task, exit_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        _event = t.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("reflex.lysosome.recv_failed", error=str(e))
                    if t is enter_task:
                        enter_task = asyncio.create_task(deep_q.get())
                        try:
                            await self._sweep()
                        except Exception as e:
                            log.exception("reflex.lysosome.sweep_failed", error=str(e))
                    elif t is exit_task:
                        exit_task = asyncio.create_task(exit_q.get())
                        self._abort.set()
        finally:
            for p in (enter_task, exit_task):
                if not p.done():
                    p.cancel()
            self._bus.unsubscribe("idle.enter", deep_q)
            self._bus.unsubscribe("idle.exit", exit_q)


def _default_pid_scanner() -> list[tuple[int, str]]:
    """Real-world scanner used in production — psutil-backed."""
    try:
        import psutil  # local import so tests don't require it at module load
    except ImportError:
        return []
    out: list[tuple[int, str]] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            info = p.info
            out.append((int(info["pid"]), str(info.get("name") or "")))
        except Exception:
            continue
    return out
