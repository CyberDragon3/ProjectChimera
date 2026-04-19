"""Tests for the Worm reflex."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.worm import WormReflex
from chimera.safety import ProtectedSpecies


@dataclass
class FakeThrottler:
    demoted: list[tuple[int, int]] = field(default_factory=list)
    succeed: bool = True

    def demote(self, pid: int, level: int) -> bool:
        self.demoted.append((pid, level))
        return self.succeed


async def _spin_until(predicate, timeout: float = 0.5) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("predicate never became true")


async def test_throttles_non_protected_spike() -> None:
    bus = Bus()
    safety = ProtectedSpecies.from_list(["explorer.exe"])
    throttler = FakeThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=100)

    q = bus.subscribe("reflex.worm.throttle")
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)  # give worm time to subscribe
        bus.publish(Event(topic="cpu.spike", payload={"pid": 1234, "exe": "stress.exe", "cpu_percent": 95.0}))
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev.payload["pid"] == 1234
        assert ev.payload["ok"] is True
        assert throttler.demoted == [(1234, throttler.demoted[0][1])]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_protected_process_is_spared() -> None:
    bus = Bus()
    safety = ProtectedSpecies.from_list(["explorer.exe"])
    throttler = FakeThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=100)

    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(topic="cpu.spike", payload={"pid": 77, "exe": "explorer.exe", "cpu_percent": 99.0}))
        await asyncio.sleep(0.05)
        assert throttler.demoted == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_intentional_foreground_suppresses_throttle() -> None:
    bus = Bus()
    safety = ProtectedSpecies.from_list([])
    throttler = FakeThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=100)

    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(topic="window.foreground", payload={"exe": "premiere.exe", "title": "Project"}))
        await asyncio.sleep(0.02)
        bus.publish(Event(topic="cpu.spike", payload={"pid": 42, "exe": "premiere.exe", "cpu_percent": 99.0}))
        await asyncio.sleep(0.08)
        assert throttler.demoted == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_reflex_fires_within_time_budget() -> None:
    bus = Bus()
    safety = ProtectedSpecies.from_list([])
    throttler = FakeThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=50)

    q = bus.subscribe("reflex.worm.throttle")
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        bus.publish(Event(topic="cpu.spike", payload={"pid": 1, "exe": "x.exe", "cpu_percent": 95.0}))
        await asyncio.wait_for(q.get(), timeout=0.3)
        elapsed_ms = (loop.time() - t0) * 1000
        # Sanity: should fire well under 300ms (plan's SLA).
        assert elapsed_ms < 300
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
