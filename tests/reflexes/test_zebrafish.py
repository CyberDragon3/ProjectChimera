"""Tests for the Zebrafish thermal governor."""

from __future__ import annotations

import asyncio
import time

import pytest

from chimera.bus import Bus
from chimera.reflexes.zebrafish import ZebrafishReflex
from chimera.store import RingBuffer


async def test_emits_rising_when_slope_exceeds_threshold() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=120)
    now = time.monotonic()
    # +5 °C/min synthetic ramp.
    for i in range(30):
        buf.append(50.0 + (5.0 / 60.0) * i, ts=now + i)

    reflex = ZebrafishReflex(bus, buf, slope_c_per_min_threshold=2.5, window_seconds=30.0, interval_ms=10)
    q = bus.subscribe("thermal.rising")
    task = asyncio.create_task(reflex.run())
    try:
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev.payload["severity"] in {"warn", "critical"}
        assert ev.payload["slope_c_per_min"] > 2.5
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_silent_when_temperature_flat() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=120)
    now = time.monotonic()
    for i in range(30):
        buf.append(50.0, ts=now + i)

    reflex = ZebrafishReflex(bus, buf, slope_c_per_min_threshold=2.5, interval_ms=10)
    q = bus.subscribe("thermal.rising")
    task = asyncio.create_task(reflex.run())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
