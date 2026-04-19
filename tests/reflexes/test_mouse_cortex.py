"""Tests for :class:`MouseCortex` — 100-neuron E/I LIF veto publisher."""

from __future__ import annotations

import asyncio
import contextlib
import time

import numpy as np

from chimera.bus import Bus, Event
from chimera.config import NeuroCfg
from chimera.reflexes.mouse import MouseCortex


def _cfg(
    *,
    tick_hz: int = 200,
    creator_drive_mv: float = 20.0,
    rate_threshold_hz: float = 5.0,
    noise_sigma_mv: float = 1.0,
    dopamine_gain_coeff: float = 0.5,
) -> NeuroCfg:
    return NeuroCfg(
        enabled=True,
        tick_hz=tick_hz,
        noise_sigma_mv=noise_sigma_mv,
        mouse_population=100,
        mouse_excitatory_frac=0.8,
        connectivity_p=0.1,
        mouse_rate_threshold_hz=rate_threshold_hz,
        mouse_creator_drive_mv=creator_drive_mv,
        dopamine_gain_coeff=dopamine_gain_coeff,
    )


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


async def test_creator_app_and_foreground_spike_triggers_protect() -> None:
    bus = Bus()
    cortex = MouseCortex(
        bus,
        neuro_cfg=_cfg(),
        rng=np.random.default_rng(42),
    )
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.3))
    task = asyncio.create_task(cortex.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "code.exe", "pid": 1001},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1001, "exe": "code.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    on_events = [e for e in events if e.payload.get("on") is True]
    assert on_events, f"expected protect_on=True, got {[e.payload for e in events]}"
    assert any(e.payload.get("foreground_pid") == 1001 for e in on_events)


async def test_non_creator_no_protect() -> None:
    bus = Bus()
    cortex = MouseCortex(
        bus,
        neuro_cfg=_cfg(),
        rng=np.random.default_rng(7),
    )
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.5))
    task = asyncio.create_task(cortex.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "notepad.exe", "pid": 1001},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1001, "exe": "notepad.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    # The only allowed "on" transition would be from a burst pushing a non-creator
    # population over threshold with pure tonic I-drive only. Without creator drive
    # the E-cells get 0 external, so they should stay silent.
    assert not any(e.payload.get("on") is True for e in events), (
        f"unexpected protect_on from non-creator app: {[e.payload for e in events]}"
    )


async def test_dopamine_raises_firing_rate() -> None:
    async def _run_once(dopamine_level: float) -> float:
        bus = Bus()
        # Set sub-threshold creator drive so that only dopamine gain pushes E over.
        cfg = _cfg(creator_drive_mv=10.0, rate_threshold_hz=50.0, noise_sigma_mv=0.5)
        cortex = MouseCortex(
            bus,
            neuro_cfg=cfg,
            rng=np.random.default_rng(123),
        )
        cortex._dopamine_level = dopamine_level  # pre-set before run
        task = asyncio.create_task(cortex.run())
        try:
            await asyncio.sleep(0.01)
            bus.publish(Event(
                topic="window.foreground",
                payload={"exe": "code.exe", "pid": 1001},
                ts=time.monotonic(),
            ))
            # Let the population integrate for a while.
            await asyncio.sleep(0.25)
            rate = float(cortex._pop.rolling_e_rate_hz)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return rate

    rate_lo = await _run_once(0.0)
    rate_hi = await _run_once(1.0)
    assert rate_hi > rate_lo, (
        f"expected higher E-rate with dopamine=1.0; got lo={rate_lo:.3f} Hz, hi={rate_hi:.3f} Hz"
    )


async def test_neuro_mouse_rate_topic_emitted() -> None:
    bus = Bus()
    cortex = MouseCortex(
        bus,
        neuro_cfg=_cfg(),
        rng=np.random.default_rng(99),
    )
    collector = asyncio.create_task(_drain(bus, "neuro.mouse.rate", 0.5))
    task = asyncio.create_task(cortex.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "code.exe", "pid": 1001},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(events) >= 5, f"expected >=5 rate events, got {len(events)}"
    for e in events:
        assert e.payload["e_rate_hz"] >= 0.0
        assert e.payload["i_rate_hz"] >= 0.0


async def test_protect_off_when_foreground_leaves_creator() -> None:
    bus = Bus()
    cortex = MouseCortex(
        bus,
        neuro_cfg=_cfg(),
        rng=np.random.default_rng(17),
    )
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 1.5))
    task = asyncio.create_task(cortex.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "code.exe", "pid": 1001},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1001, "exe": "code.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        # Wait long enough to ensure protect_on has been published.
        await asyncio.sleep(0.3)
        # Now switch to a non-creator app. Should publish protect_off.
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "notepad.exe", "pid": 2002},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    on_events = [e for e in events if e.payload.get("on") is True]
    off_events = [e for e in events if e.payload.get("on") is False]
    assert on_events, "expected at least one protect_on=True before switching"
    assert off_events, "expected protect_on=False transition after switching"
    # Ensure an off event occurred AFTER the first on event (time-ordered).
    first_on_ts = on_events[0].ts
    assert any(e.ts >= first_on_ts for e in off_events)
