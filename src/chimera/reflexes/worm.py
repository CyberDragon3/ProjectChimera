"""Worm reflex — CPU pain response with hierarchical veto gating.

Precedence (design §4.2):
  1. safety.gate  (protected species always wins)
  2. thermal.critical — supreme override, ignores protection
  3. cortex.protect_foreground — Mouse-issued stand-down per-PID
  4. exe-level intentional hint (legacy window.foreground creator-apps)
  5. demote
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

import psutil
import structlog

from chimera.bus import Bus, Event
from chimera.reflexes.openworm import OpenWormDrive
from chimera.safety import ProtectedSpecies

log = structlog.get_logger(__name__)

try:
    _BELOW_NORMAL = psutil.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]
    _IDLE_PRIO = psutil.IDLE_PRIORITY_CLASS  # type: ignore[attr-defined]
except AttributeError:
    _BELOW_NORMAL = 10
    _IDLE_PRIO = 19


class Throttler(Protocol):
    def demote(self, pid: int, level: int) -> bool: ...


class PsutilThrottler:
    def demote(self, pid: int, level: int) -> bool:
        try:
            p = psutil.Process(pid)
            p.nice(level)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.warning("worm.demote_failed", pid=pid, error=str(e))
            return False


@dataclass(slots=True)
class _CooldownState:
    until: float
    strikes: int
    reason: str


class WormReflex:
    def __init__(
        self,
        bus: Bus,
        safety: ProtectedSpecies,
        throttler: Throttler,
        openworm: OpenWormDrive | None = None,
        deadline_ms: int = 10,
        success_cooldown_s: float = 2.0,
        protect_cooldown_s: float = 3.0,
        intentional_cooldown_s: float = 5.0,
        denied_cooldown_s: float = 6.0,
        max_cooldown_s: float = 30.0,
    ) -> None:
        self._bus = bus
        self._safety = safety
        self._throttler = throttler
        self._deadline = deadline_ms / 1000.0
        self._openworm = openworm
        self._success_cooldown_s = success_cooldown_s
        self._protect_cooldown_s = protect_cooldown_s
        self._intentional_cooldown_s = intentional_cooldown_s
        self._denied_cooldown_s = denied_cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self._intentional: dict[str, float] = {}
        self._pid_cooldowns: dict[int, _CooldownState] = {}
        self._exe_cooldowns: dict[str, _CooldownState] = {}
        self._thermal_critical = False
        self._protect_on = False
        self._protect_pid: int | None = None

    def _throttle_level(self, pid: int, exe: str, cpu_percent: float) -> tuple[int, dict[str, object]]:
        if self._thermal_critical:
            return _IDLE_PRIO, {
                "level_name": "idle",
                "confidence": 1.0,
                "openworm_active_fraction": 1.0,
            }
        if self._openworm is None or not self._openworm.available:
            return _BELOW_NORMAL, {
                "level_name": "below_normal",
                "confidence": 0.0,
                "openworm_active_fraction": 0.0,
            }
        state = self._openworm.model_state(
            "spike",
            pid=pid,
            exe=exe,
            cpu_percent=cpu_percent,
        )
        level_name = str(state["throttle_level"])
        level = _IDLE_PRIO if level_name == "idle" else _BELOW_NORMAL
        return level, {
            "level_name": level_name,
            "confidence": float(state["confidence"]),
            "openworm_active_fraction": float(state["active_fraction"]),
            "openworm_active_count": int(state["active_count"]),
        }

    def mark_intentional(self, exe: str, ttl_seconds: float = 10.0) -> None:
        self._intentional[exe.lower()] = time.monotonic() + ttl_seconds

    def _is_intentional(self, exe: str) -> bool:
        expiry = self._intentional.get(exe.lower())
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            self._intentional.pop(exe.lower(), None)
            return False
        return True

    def _cooldown_state(self, pid: int, exe: str) -> _CooldownState | None:
        now = time.monotonic()
        exe_key = exe.lower()
        pid_state = self._pid_cooldowns.get(pid)
        if pid_state is not None and pid_state.until <= now:
            self._pid_cooldowns.pop(pid, None)
            pid_state = None
        exe_state = self._exe_cooldowns.get(exe_key)
        if exe_state is not None and exe_state.until <= now:
            self._exe_cooldowns.pop(exe_key, None)
            exe_state = None
        if pid_state is None:
            return exe_state
        if exe_state is None:
            return pid_state
        return pid_state if pid_state.until >= exe_state.until else exe_state

    def _remember(self, *, pid: int | None, exe: str | None, reason: str, base_seconds: float) -> None:
        now = time.monotonic()
        if pid is not None:
            prev = self._pid_cooldowns.get(pid)
            strikes = 1 if prev is None or prev.reason != reason else prev.strikes + 1
            seconds = min(self._max_cooldown_s, base_seconds * (1.8 ** (strikes - 1)))
            self._pid_cooldowns[pid] = _CooldownState(until=now + seconds, strikes=strikes, reason=reason)
        if exe:
            exe_key = exe.lower()
            prev = self._exe_cooldowns.get(exe_key)
            strikes = 1 if prev is None or prev.reason != reason else prev.strikes + 1
            seconds = min(self._max_cooldown_s, base_seconds * (1.8 ** (strikes - 1)))
            self._exe_cooldowns[exe_key] = _CooldownState(until=now + seconds, strikes=strikes, reason=reason)

    def _forget(self, *, pid: int | None = None, exe: str | None = None) -> None:
        if pid is not None:
            self._pid_cooldowns.pop(pid, None)
        if exe:
            self._exe_cooldowns.pop(exe.lower(), None)

    async def _handle(self, event: Event) -> None:
        exe = str(event.payload.get("exe", ""))
        try:
            pid = int(event.payload.get("pid", -1))
            pct = float(event.payload.get("cpu_percent", 0.0))
        except (TypeError, ValueError):
            log.warning("reflex.worm.bad_payload", payload=event.payload)
            return

        cooldown = self._cooldown_state(pid, exe)
        if cooldown is not None:
            log.info(
                "reflex.worm.cooldown_skip",
                pid=pid,
                exe=exe,
                cpu_percent=pct,
                reason=cooldown.reason,
                retry_in_ms=max(0, round((cooldown.until - time.monotonic()) * 1000)),
            )
            return

        if self._safety.is_protected(exe, pid=pid):
            self._remember(pid=pid, exe=exe, reason="protected_species", base_seconds=self._denied_cooldown_s)
            self._safety.gate(exe, action="throttle", pid=pid)
            return

        if self._thermal_critical:
            self._forget(pid=pid, exe=exe)
            log.warning(
                "reflex.worm.critical_override",
                pid=pid, exe=exe, cpu_percent=pct,
            )
        else:
            if self._protect_on and self._protect_pid == pid:
                self._remember(pid=pid, exe=None, reason="protect_foreground", base_seconds=self._protect_cooldown_s)
                log.info(
                    "reflex.worm.stand_down_foreground",
                    pid=pid, exe=exe, cpu_percent=pct,
                )
                return
            if self._is_intentional(exe):
                self._remember(pid=None, exe=exe, reason="intentional", base_seconds=self._intentional_cooldown_s)
                log.info("worm.skip.intentional", pid=pid, exe=exe, cpu_percent=pct)
                return

        level, modulation = self._throttle_level(pid, exe, pct)
        ok = self._throttler.demote(pid, level)
        self._remember(
            pid=pid,
            exe=None,
            reason="throttled" if ok else "demote_failed",
            base_seconds=self._success_cooldown_s,
        )
        self._bus.publish(
            Event(
                topic="reflex.worm.throttle",
                payload={
                    "pid": pid,
                    "exe": exe,
                    "cpu_percent": pct,
                    "ok": ok,
                    "critical": self._thermal_critical,
                    "level": modulation["level_name"],
                    "confidence": modulation["confidence"],
                    "openworm_active_fraction": modulation["openworm_active_fraction"],
                    "openworm_active_count": modulation.get("openworm_active_count", 0),
                },
                ts=time.monotonic(),
            )
        )
        log.info(
            "reflex.worm.throttle",
            pid=pid, exe=exe, cpu_percent=pct, ok=ok,
            critical=self._thermal_critical,
            level=modulation["level_name"],
            confidence=modulation["confidence"],
            openworm_active_fraction=modulation["openworm_active_fraction"],
        )

    async def run(self) -> None:
        q = self._bus.subscribe("cpu.spike")
        win_q = self._bus.subscribe("window.foreground")
        crit_q = self._bus.subscribe("thermal.critical")
        prot_q = self._bus.subscribe("cortex.protect_foreground")
        log.info("reflex.worm.start", deadline_ms=int(self._deadline * 1000))
        spike_task: asyncio.Task[Event] = asyncio.create_task(q.get())
        win_task: asyncio.Task[Event] = asyncio.create_task(win_q.get())
        crit_task: asyncio.Task[Event] = asyncio.create_task(crit_q.get())
        prot_task: asyncio.Task[Event] = asyncio.create_task(prot_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {spike_task, win_task, crit_task, prot_task},
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
                    elif t is crit_task:
                        crit_task = asyncio.create_task(crit_q.get())
                    elif t is prot_task:
                        prot_task = asyncio.create_task(prot_q.get())

                    if event is None:
                        continue

                    if event.topic.startswith("window"):
                        exe = str(event.payload.get("exe", ""))
                        if exe:
                            self.mark_intentional(exe)
                        continue

                    if event.topic == "thermal.critical":
                        self._thermal_critical = bool(event.payload.get("on"))
                        continue

                    if event.topic == "cortex.protect_foreground":
                        self._protect_on = bool(event.payload.get("on"))
                        fg = event.payload.get("foreground_pid")
                        try:
                            self._protect_pid = int(fg) if fg is not None else None
                        except (TypeError, ValueError):
                            self._protect_pid = None
                        continue

                    try:
                        await asyncio.wait_for(self._handle(event), timeout=self._deadline)
                    except asyncio.TimeoutError:
                        log.warning("reflex.worm.deadline_exceeded", event=event.topic)
                    except Exception as e:
                        log.exception("reflex.worm.handler_failed", error=str(e))
        finally:
            for pending in (spike_task, win_task, crit_task, prot_task):
                if not pending.done():
                    pending.cancel()
            self._bus.unsubscribe("cpu.spike", q)
            self._bus.unsubscribe("window.foreground", win_q)
            self._bus.unsubscribe("thermal.critical", crit_q)
            self._bus.unsubscribe("cortex.protect_foreground", prot_q)
