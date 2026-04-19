"""Tests for the CPU / Idle sensors with fake backends."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from chimera.bus import Bus
from chimera.sensors.base import ProcSample
from chimera.sensors.cpu import CpuSensor
from chimera.sensors.idle import IdleSensor


@dataclass
class FakeCpu:
    samples: list[ProcSample]

    def iter_process_samples(self):
        return iter(self.samples)


@dataclass
class FakeIdle:
    value: float = 0.0

    def idle_seconds(self) -> float:
        return self.value


async def test_cpu_sensor_publishes_only_spikes() -> None:
    bus = Bus()
    backend = FakeCpu(
        [
            ProcSample(pid=1, exe="calm.exe", cpu_percent=5.0, rss_bytes=100),
            ProcSample(pid=2, exe="hog.exe", cpu_percent=95.0, rss_bytes=200),
        ]
    )
    sensor = CpuSensor(bus, backend, interval_ms=10, spike_percent=80.0)
    q = bus.subscribe("cpu.spike")
    task = asyncio.create_task(sensor.run())
    try:
        ev = await asyncio.wait_for(q.get(), timeout=0.2)
        assert ev.payload["pid"] == 2
        assert ev.payload["exe"] == "hog.exe"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_idle_sensor_emits_enter_then_exit() -> None:
    bus = Bus()
    backend = FakeIdle(value=0.0)
    sensor = IdleSensor(bus, backend, interval_ms=10, idle_threshold_seconds=1.0)
    q = bus.subscribe("idle")
    task = asyncio.create_task(sensor.run())
    try:
        await asyncio.sleep(0.02)
        backend.value = 5.0
        ev = await asyncio.wait_for(q.get(), timeout=0.2)
        assert ev.topic == "idle.enter"
        backend.value = 0.0
        ev = await asyncio.wait_for(q.get(), timeout=0.2)
        assert ev.topic == "idle.exit"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
