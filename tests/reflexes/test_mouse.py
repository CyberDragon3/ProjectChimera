"""Tests for the Mouse semantic filter."""

from __future__ import annotations

import asyncio

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.mouse import MouseReflex


async def test_classifies_creator_app_as_intentional() -> None:
    bus = Bus()
    mouse = MouseReflex(bus, creator_apps=frozenset({"blender.exe"}))
    q = bus.subscribe("context.active_window")
    task = asyncio.create_task(mouse.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(topic="window.foreground", payload={"exe": "blender.exe", "title": "scene"}))
        ev = await asyncio.wait_for(q.get(), timeout=0.2)
        assert ev.payload["intentional"] is True
        assert ev.payload["exe"] == "blender.exe"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_non_creator_app_is_not_intentional() -> None:
    bus = Bus()
    mouse = MouseReflex(bus, creator_apps=frozenset({"blender.exe"}))
    q = bus.subscribe("context.active_window")
    task = asyncio.create_task(mouse.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(topic="window.foreground", payload={"exe": "chrome.exe", "title": "x"}))
        ev = await asyncio.wait_for(q.get(), timeout=0.2)
        assert ev.payload["intentional"] is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
