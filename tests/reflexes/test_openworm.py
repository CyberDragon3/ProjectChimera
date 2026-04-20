from __future__ import annotations

import asyncio
import contextlib

from chimera.bus import Bus, Event
from chimera.reflexes.openworm import OpenWormReflex


async def test_openworm_publishes_initial_state() -> None:
    bus = Bus()
    reflex = OpenWormReflex(bus, neuron_names=("ADAL", "ADAR", "ADEL"))
    q = bus.subscribe("neuro.worm.state")
    task = asyncio.create_task(reflex.run())
    try:
        event = await asyncio.wait_for(q.get(), timeout=0.5)
        assert event.payload["available"] is True
        assert event.payload["neuron_count"] == 3
        assert event.payload["status"] == "idle"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        bus.unsubscribe("neuro.worm.state", q)


async def test_openworm_maps_spike_and_throttle_to_real_neurons() -> None:
    bus = Bus()
    names = ("ADAL", "ADAR", "ADEL", "ADER", "AFDL", "AFDR")
    reflex = OpenWormReflex(bus, neuron_names=names)
    q = bus.subscribe("neuro.worm.state")
    task = asyncio.create_task(reflex.run())
    try:
        await asyncio.wait_for(q.get(), timeout=0.5)
        bus.publish(
            Event(
                topic="cpu.spike",
                payload={"pid": 42, "exe": "stress.exe", "cpu_percent": 90.0},
                ts=1.0,
            )
        )
        spike = await asyncio.wait_for(q.get(), timeout=0.5)
        assert spike.payload["status"] == "spike"
        assert spike.payload["active_count"] > 0
        assert set(spike.payload["active_neurons"]).issubset(set(names))

        bus.publish(
            Event(
                topic="reflex.worm.throttle",
                payload={"pid": 42, "exe": "stress.exe", "cpu_percent": 90.0, "ok": True},
                ts=2.0,
            )
        )
        throttle = await asyncio.wait_for(q.get(), timeout=0.5)
        assert throttle.payload["status"] == "throttle"
        assert throttle.payload["active_count"] >= spike.payload["active_count"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        bus.unsubscribe("neuro.worm.state", q)
