"""Tests for the Worm reflex."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.openworm import OpenWormDrive
from chimera.reflexes.worm import WormReflex, _BELOW_NORMAL, _IDLE_PRIO
from chimera.safety import ProtectedSpecies


@dataclass
class FakeThrottler:
    demoted: list[tuple[int, int]] = field(default_factory=list)
    succeed: bool = True

    def demote(self, pid: int, level: int) -> bool:
        self.demoted.append((pid, level))
        return self.succeed


class CountingSafety(ProtectedSpecies):
    def __init__(self, protected: list[str]) -> None:
        super().__init__(frozenset(protected))
        self.gate_calls: list[tuple[str, int | None, str]] = []

    def gate(self, exe_name: str, action: str, pid: int | None = None) -> bool:
        self.gate_calls.append((exe_name, pid, action))
        return super().gate(exe_name, action=action, pid=pid)


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


async def test_openworm_can_raise_throttle_strength() -> None:
    bus = Bus()
    safety = ProtectedSpecies.from_list([])
    throttler = FakeThrottler()
    openworm = OpenWormDrive(
        neuron_names=tuple(f"N{i:03d}" for i in range(302))
    )
    worm = WormReflex(bus, safety, throttler, openworm=openworm, deadline_ms=100)

    q = bus.subscribe("reflex.worm.throttle")
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(topic="cpu.spike", payload={"pid": 7, "exe": "hog.exe", "cpu_percent": 95.0}))
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev.payload["level"] in {"below_normal", "idle"}
        assert ev.payload["confidence"] > 0.0
        assert ev.payload["openworm_active_fraction"] > 0.0
        assert throttler.demoted[0][1] in {_BELOW_NORMAL, _IDLE_PRIO}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_protected_process_learns_short_cooldown_after_denial() -> None:
    bus = Bus()
    safety = CountingSafety(["explorer.exe"])
    throttler = FakeThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=100, denied_cooldown_s=0.2)

    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        event = Event(topic="cpu.spike", payload={"pid": 77, "exe": "explorer.exe", "cpu_percent": 99.0})
        bus.publish(event)
        await asyncio.sleep(0.03)
        bus.publish(event)
        await asyncio.sleep(0.05)
        assert throttler.demoted == []
        assert safety.gate_calls == [("explorer.exe", 77, "throttle")]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_recently_throttled_pid_is_not_rehit_until_cooldown_expires() -> None:
    bus = Bus()
    safety = ProtectedSpecies.from_list([])
    throttler = FakeThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=100, success_cooldown_s=0.08)

    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        event = Event(topic="cpu.spike", payload={"pid": 1234, "exe": "stress.exe", "cpu_percent": 95.0})
        bus.publish(event)
        await _spin_until(lambda: len(throttler.demoted) == 1)
        bus.publish(event)
        await asyncio.sleep(0.03)
        assert len(throttler.demoted) == 1
        await asyncio.sleep(0.07)
        bus.publish(event)
        await _spin_until(lambda: len(throttler.demoted) == 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
