"""Lysosome scavenger — backend contract + sweep behavior."""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterable

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.lysosome import (
    LysosomeBackend,
    LysosomeReflex,
    NullLysosomeBackend,
)
from chimera.safety import ProtectedSpecies


def test_null_backend_is_noop():
    b: LysosomeBackend = NullLysosomeBackend()
    assert b.trim_working_set([1, 2, 3]) == 0
    assert b.flush_system_cache() is None
    assert b.kill(1234) is False


class _RecordingBackend:
    def __init__(self, kill_succeeds: bool = True) -> None:
        self.trims: list[list[int]] = []
        self.flushes: int = 0
        self.kills: list[int] = []
        self._kill_ok = kill_succeeds

    def trim_working_set(self, pids: Iterable[int]) -> int:
        p = list(pids)
        self.trims.append(p)
        return len(p)

    def flush_system_cache(self) -> int | None:
        self.flushes += 1
        return 0

    def kill(self, pid: int) -> bool:
        self.kills.append(pid)
        return self._kill_ok


def _make(
    backend: LysosomeBackend,
    *,
    targets: tuple[str, ...] = (),
    enabled: bool = True,
    interval: int = 0,
) -> tuple[Bus, LysosomeReflex]:
    bus = Bus()
    safety = ProtectedSpecies.from_list(["winlogon.exe"])

    def fake_scan() -> list[tuple[int, str]]:
        return [
            (1234, "hog.exe"),
            (9999, "winlogon.exe"),
            (4242, "crash_handler.exe"),
        ]

    r = LysosomeReflex(
        bus,
        safety,
        backend,
        enabled=enabled,
        sweep_interval_seconds=interval,
        targets=targets,
        pid_scanner=fake_scan,
    )
    return bus, r


async def test_disabled_performs_no_sweep():
    bus, r = _make(_RecordingBackend(), enabled=False)
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.03)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert r.sweep_count == 0


async def test_idle_enter_triggers_phases_1_and_2():
    backend = _RecordingBackend()
    bus, r = _make(backend)
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(backend.trims) == 1
    assert 9999 not in backend.trims[0]  # protected species
    assert 1234 in backend.trims[0]
    assert backend.flushes == 1
    assert backend.kills == []


async def test_empty_targets_skips_phase_3():
    backend = _RecordingBackend()
    bus, r = _make(backend, targets=())
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert backend.kills == []


async def test_target_kill_is_safety_gated():
    backend = _RecordingBackend()
    bus, r = _make(backend, targets=("crash_handler.exe", "winlogon.exe"))
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert backend.kills == [4242]  # winlogon.exe blocked by safety


async def test_rate_limit_prevents_double_sweep():
    backend = _RecordingBackend()
    bus, r = _make(backend, interval=60)
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.03)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.03)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert r.sweep_count == 1
