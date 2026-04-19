"""Window sensor publishes pid alongside exe + title so Mouse's
cortex.protect_foreground can match against cpu.spike payloads.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from chimera.bus import Bus, Event
from chimera.sensors.window import NullWindowBackend, WindowSensor


class _FakeBackend:
    def __init__(self, seq: list[tuple[str, str, int | None]]) -> None:
        self._seq = list(seq)

    def foreground(self) -> tuple[str, str, int | None]:
        if self._seq:
            return self._seq.pop(0)
        return ("", "", None)


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


async def test_window_sensor_publishes_pid_in_payload():
    bus = Bus()
    backend = _FakeBackend([("chrome.exe", "Google", 1234)])
    sensor = WindowSensor(bus, backend, interval_ms=10)
    collector = asyncio.create_task(_drain(bus, "window.foreground", 0.08))
    task = asyncio.create_task(sensor.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert any(e.payload.get("pid") == 1234 for e in events)
    assert any(e.payload.get("exe") == "chrome.exe" for e in events)
    assert any(e.payload.get("title") == "Google" for e in events)


async def test_window_sensor_suppresses_empty_exe():
    bus = Bus()
    backend = _FakeBackend([("", "", None)])
    sensor = WindowSensor(bus, backend, interval_ms=10)
    collector = asyncio.create_task(_drain(bus, "window.foreground", 0.04))
    task = asyncio.create_task(sensor.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert events == []


async def test_window_sensor_deduplicates_identical_readings():
    bus = Bus()
    # Backend reports the same tuple every poll.
    backend = _FakeBackend([("chrome.exe", "t", 5)] * 20)
    sensor = WindowSensor(bus, backend, interval_ms=5)
    collector = asyncio.create_task(_drain(bus, "window.foreground", 0.06))
    task = asyncio.create_task(sensor.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(events) == 1


def test_null_backend_returns_triple():
    b = NullWindowBackend()
    assert b.foreground() == ("", "", None)
