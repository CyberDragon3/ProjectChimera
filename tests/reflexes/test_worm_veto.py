"""Worm veto precedence — thermal.critical > cortex.protect_foreground > default."""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.worm import WormReflex
from chimera.safety import ProtectedSpecies


class _RecordingThrottler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
    def demote(self, pid: int, level: int) -> bool:
        self.calls.append((pid, level))
        return True


def _make() -> tuple[Bus, ProtectedSpecies, _RecordingThrottler, WormReflex]:
    bus = Bus()
    safety = ProtectedSpecies.from_list(["winlogon.exe"])
    throttler = _RecordingThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=50)
    return bus, safety, throttler, worm


async def test_default_path_demotes():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "hog.exe", "cpu_percent": 90.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(throttler.calls) == 1
    assert throttler.calls[0][0] == 1234


async def test_protect_foreground_stands_down():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": True, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert throttler.calls == []


async def test_thermal_critical_overrides_protect_foreground():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": True, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        bus.publish(Event(
            topic="thermal.critical",
            payload={"on": True, "celsius": 97.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(throttler.calls) == 1
    assert throttler.calls[0][0] == 1234


async def test_protected_species_never_demoted_even_under_critical():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="thermal.critical",
            payload={"on": True, "celsius": 97.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 7, "exe": "winlogon.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert throttler.calls == []


async def test_protect_clears_when_off_event_arrives():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": True, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": False, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(throttler.calls) == 1
