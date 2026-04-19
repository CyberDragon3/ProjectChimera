"""Zebrafish critical state machine — hard-floor temperature veto."""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.zebrafish import ZebrafishReflex
from chimera.store import RingBuffer


async def _collect(bus: Bus, topic: str, duration: float) -> list[Event]:
    q = bus.subscribe(topic)
    events: list[Event] = []
    try:
        async with asyncio.timeout(duration):
            while True:
                events.append(await q.get())
    except TimeoutError:
        pass
    finally:
        bus.unsubscribe(topic, q)
    return events


async def test_critical_fires_after_n_consecutive_samples_above_threshold():
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    for c in (96.0, 97.0):
        buf.append(c)

    z = ZebrafishReflex(
        bus, buf,
        slope_c_per_min_threshold=2.5,
        critical_c=95.0, critical_clear_c=90.0, critical_samples=2,
        max_hold_seconds=300,
        interval_ms=10,
    )
    collector = asyncio.create_task(_collect(bus, "thermal.critical", 0.08))
    task = asyncio.create_task(z.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert any(e.payload.get("on") is True for e in events)


async def test_critical_clears_after_n_consecutive_samples_below_clear():
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    for c in (96.0, 97.0, 80.0, 79.0):
        buf.append(c)

    z = ZebrafishReflex(
        bus, buf,
        slope_c_per_min_threshold=2.5,
        critical_c=95.0, critical_clear_c=90.0, critical_samples=2,
        max_hold_seconds=300,
        interval_ms=10,
    )
    z._critical_on = True  # force starting state

    collector = asyncio.create_task(_collect(bus, "thermal.critical", 0.08))
    task = asyncio.create_task(z.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert any(e.payload.get("on") is False for e in events)


async def test_critical_does_not_flap_between_clear_and_critical():
    """Mid-range samples (91-92 °C) should neither fire nor clear."""
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    for c in (91.0, 91.5, 92.0, 92.5):
        buf.append(c)

    z = ZebrafishReflex(
        bus, buf,
        slope_c_per_min_threshold=2.5,
        critical_c=95.0, critical_clear_c=90.0, critical_samples=2,
        max_hold_seconds=300,
        interval_ms=10,
    )
    collector = asyncio.create_task(_collect(bus, "thermal.critical", 0.08))
    task = asyncio.create_task(z.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert events == []
