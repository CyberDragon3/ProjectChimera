"""Mouse publishes cortex.protect_foreground when foreground PID == CPU-spike PID."""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.mouse import MouseReflex


async def _drain(bus: Bus, topic: str, duration: float) -> list[Event]:
    q = bus.subscribe(topic)
    out: list[Event] = []
    try:
        async with asyncio.timeout(duration):
            while True:
                out.append(await q.get())
    except TimeoutError:
        pass
    finally:
        bus.unsubscribe(topic, q)
    return out


async def test_spike_on_foreground_publishes_protect_on():
    bus = Bus()
    m = MouseReflex(bus)
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.1))
    task = asyncio.create_task(m.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "chrome.exe", "pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0, "rss_bytes": 100},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    on_events = [e for e in events if e.payload.get("on") is True]
    assert any(e.payload.get("foreground_pid") == 1234 for e in on_events)


async def test_spike_on_non_foreground_does_not_publish_protect_on():
    bus = Bus()
    m = MouseReflex(bus)
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.1))
    task = asyncio.create_task(m.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "chrome.exe", "pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 9999, "exe": "cc.exe", "cpu_percent": 92.0, "rss_bytes": 100},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert not any(e.payload.get("on") is True for e in events)


async def test_foreground_change_publishes_protect_off():
    bus = Bus()
    m = MouseReflex(bus)
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.1))
    task = asyncio.create_task(m.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "chrome.exe", "pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "code.exe", "pid": 5678},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    off_events = [e for e in events if e.payload.get("on") is False]
    assert len(off_events) >= 2
