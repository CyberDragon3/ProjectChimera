"""Tests for the Fly reflex (idle → arousal re-shape)."""

from __future__ import annotations

import asyncio

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.fly import FlyReflex


async def test_idle_enter_emits_away() -> None:
    bus = Bus()
    fly = FlyReflex(bus)
    q = bus.subscribe("arousal")
    task = asyncio.create_task(fly.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(topic="idle.enter", payload={"seconds": 400}))
        ev = await asyncio.wait_for(q.get(), timeout=0.2)
        assert ev.topic == "arousal.away"
        assert ev.payload["seconds"] == 400
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_idle_exit_emits_present() -> None:
    bus = Bus()
    fly = FlyReflex(bus)
    q = bus.subscribe("arousal")
    task = asyncio.create_task(fly.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(topic="idle.exit", payload={"seconds": 2}))
        ev = await asyncio.wait_for(q.get(), timeout=0.2)
        assert ev.topic == "arousal.present"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
