"""Tests for chimera.neuro.dopamine — scalar reward modulator."""

from __future__ import annotations

import asyncio
import time

import pytest

from chimera.bus import Bus, Event
from chimera.neuro.dopamine import DopamineConfig, DopamineModulator


def _throttle_event(pid: int, *, ok: bool = True, critical: bool = False) -> Event:
    return Event(
        topic="reflex.worm.throttle",
        payload={
            "pid": pid,
            "exe": f"proc_{pid}.exe",
            "cpu_percent": 95.0,
            "ok": ok,
            "critical": critical,
        },
        ts=time.monotonic(),
    )


def _spike_event(pid: int) -> Event:
    return Event(
        topic="cpu.spike",
        payload={
            "pid": pid,
            "exe": f"proc_{pid}.exe",
            "cpu_percent": 95.0,
            "rss_bytes": 1 << 20,
        },
        ts=time.monotonic(),
    )


def _protect_event(pid: int | None, *, on: bool = True) -> Event:
    return Event(
        topic="cortex.protect_foreground",
        payload={"on": on, "foreground_pid": pid},
        ts=time.monotonic(),
    )


async def _run(mod: DopamineModulator) -> tuple[asyncio.Task[None], asyncio.Event]:
    stop = asyncio.Event()
    task = asyncio.create_task(mod.run(stop))
    # let subscriptions register
    await asyncio.sleep(0.05)
    return task, stop


async def _stop(task: asyncio.Task[None], stop: asyncio.Event) -> None:
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except TimeoutError:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task


async def test_hit_raises_level() -> None:
    bus = Bus()
    cfg = DopamineConfig(
        hit_delta=0.2, hit_window_s=0.3, emit_interval_s=0.05, decay_per_sec=0.0
    )
    mod = DopamineModulator(bus, cfg)
    task, stop = await _run(mod)
    try:
        bus.publish(_protect_event(1001))
        await asyncio.sleep(0.05)
        bus.publish(_throttle_event(999))  # not foreground -> eligible
        # wait for window + buffer so sweeper matures it into a hit
        await asyncio.sleep(cfg.hit_window_s + 0.3)
        assert mod.level > 0.1
        assert mod._state.last_outcome == "hit"
    finally:
        await _stop(task, stop)


async def test_miss_drops_level() -> None:
    bus = Bus()
    cfg = DopamineConfig(
        hit_delta=0.2,
        miss_delta=0.25,
        hit_window_s=0.5,
        emit_interval_s=0.05,
        decay_per_sec=0.0,
    )
    mod = DopamineModulator(bus, cfg)
    mod.level = 0.5  # prime
    task, stop = await _run(mod)
    try:
        bus.publish(_protect_event(1001))
        await asyncio.sleep(0.05)
        bus.publish(_throttle_event(999))
        await asyncio.sleep(0.05)
        bus.publish(_spike_event(1001))  # foreground spike within window
        await asyncio.sleep(0.2)
        # Level should have dropped ~miss_delta from 0.5 -> ~0.25.
        assert mod.level == pytest.approx(0.5 - cfg.miss_delta, abs=0.01)
        assert mod._state.last_outcome == "miss"
    finally:
        await _stop(task, stop)


async def test_decay_towards_zero() -> None:
    bus = Bus()
    cfg = DopamineConfig(decay_per_sec=0.5, emit_interval_s=0.1, hit_window_s=10.0)
    mod = DopamineModulator(bus, cfg)
    mod.level = 1.0
    task, stop = await _run(mod)
    try:
        await asyncio.sleep(2.0)
        assert mod.level < 0.5
    finally:
        await _stop(task, stop)


async def test_hit_rate_computed() -> None:
    bus = Bus()
    mod = DopamineModulator(bus, DopamineConfig())
    mod.record_outcome("hit")
    mod.record_outcome("hit")
    mod.record_outcome("hit")
    mod.record_outcome("miss")
    assert mod.hit_rate == pytest.approx(0.75)


async def test_emits_neuro_dopamine_topic() -> None:
    bus = Bus()
    cfg = DopamineConfig(emit_interval_s=0.2, decay_per_sec=0.0, hit_window_s=10.0)
    mod = DopamineModulator(bus, cfg)
    q = bus.subscribe("neuro.dopamine")
    task, stop = await _run(mod)
    try:
        await asyncio.sleep(1.5)
    finally:
        await _stop(task, stop)
    events: list[Event] = []
    while not q.empty():
        events.append(q.get_nowait())
    assert len(events) >= 5
    for e in events:
        assert e.topic == "neuro.dopamine"
        assert set(e.payload.keys()) >= {"level", "hit_rate", "last_outcome"}
        assert 0.0 <= float(e.payload["level"]) <= 1.0


async def test_stops_on_event() -> None:
    bus = Bus()
    mod = DopamineModulator(bus, DopamineConfig(emit_interval_s=0.1))
    stop = asyncio.Event()
    task = asyncio.create_task(mod.run(stop))
    await asyncio.sleep(0.1)
    start = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=0.5)
    assert (time.monotonic() - start) < 0.5
