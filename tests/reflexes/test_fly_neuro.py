"""Tests for the LIF-driven FlyNeuroReflex."""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from chimera.bus import Bus, Event
from chimera.config import NeuroCfg
from chimera.reflexes.fly import FlyNeuroReflex


def _cfg(**overrides) -> NeuroCfg:
    base = dict(
        enabled=True,
        tick_hz=100,
        tau_m_ms=20.0,
        v_rest_mv=-65.0,
        v_reset_mv=-70.0,
        v_thresh_mv=-50.0,
        refractory_ms=2.0,
        noise_sigma_mv=0.5,
        fly_input_gain=0.5,
    )
    base.update(overrides)
    return NeuroCfg(**base)


async def _drain(q: asyncio.Queue[Event], topic: str, timeout: float) -> list[Event]:
    """Collect all events with ``topic`` that arrive on ``q`` within ``timeout``."""
    events: list[Event] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            ev = await asyncio.wait_for(q.get(), timeout=remaining)
        except TimeoutError:
            break
        if ev.topic == topic:
            events.append(ev)
    return events


async def test_fires_arousal_away_after_idle_enter() -> None:
    bus = Bus()
    rng = np.random.default_rng(42)
    reflex = FlyNeuroReflex(bus, neuro_cfg=_cfg(), rng=rng)
    q = bus.subscribe("arousal.away")
    task = asyncio.create_task(reflex.run())
    try:
        await asyncio.sleep(0.02)
        bus.publish(Event(topic="idle.enter", payload={"seconds": 300.0}))
        events = await _drain(q, "arousal.away", timeout=0.8)
        assert len(events) >= 1, "FlyNeuroReflex should fire arousal.away in an idle session"
        assert events[0].payload["seconds"] >= 0.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_idle_exit_resets_and_publishes_present() -> None:
    bus = Bus()
    rng = np.random.default_rng(7)
    reflex = FlyNeuroReflex(bus, neuro_cfg=_cfg(), rng=rng)
    present_q = bus.subscribe("arousal.present")
    away_q = bus.subscribe("arousal.away")
    task = asyncio.create_task(reflex.run())
    try:
        await asyncio.sleep(0.02)
        bus.publish(Event(topic="idle.enter", payload={"seconds": 300.0}))
        # First idle session fires at least once.
        first_aways = await _drain(away_q, "arousal.away", timeout=0.6)
        assert first_aways, "first session should fire arousal.away"
        t_first_spike = first_aways[0].ts

        bus.publish(Event(topic="idle.exit", payload={"seconds": 300.0}))
        present = await asyncio.wait_for(present_q.get(), timeout=0.3)
        assert present.topic == "arousal.present"
        assert present.payload["seconds"] == 300.0

        # Drain any lingering spikes from the first session.
        await asyncio.sleep(0.05)
        while not away_q.empty():
            away_q.get_nowait()

        # Between idle.exit and the next idle.enter, no further away spikes
        # should fire — idle counter must have been cleared.
        quiescent_aways = await _drain(away_q, "arousal.away", timeout=0.2)
        assert not quiescent_aways, (
            "arousal.away fired while not idle — idle counter did not reset"
        )

        # A new idle session must re-arm the one-shot latch and fire exactly
        # once more. If _has_fired had not been cleared, we'd get zero events.
        bus.publish(Event(topic="idle.enter", payload={"seconds": 300.0}))
        second_aways = await _drain(away_q, "arousal.away", timeout=0.8)
        assert second_aways, "second session should also fire arousal.away"
        # Exactly one arousal.away per session (no chatter).
        assert len(second_aways) == 1
        # And time since t_first_spike is positive (monotonic sanity).
        assert second_aways[0].ts > t_first_spike
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_no_fire_when_awake() -> None:
    bus = Bus()
    rng = np.random.default_rng(1234)
    reflex = FlyNeuroReflex(bus, neuro_cfg=_cfg(), rng=rng)
    q = bus.subscribe("arousal.away")
    task = asyncio.create_task(reflex.run())
    try:
        # No idle.enter published. Run for 1 s of wallclock.
        await asyncio.sleep(1.0)
        assert q.empty(), "arousal.away must not fire while awake"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_deterministic_with_seeded_rng() -> None:
    """Two reflexes with identical seed + events fire at the same tick (±1)."""

    async def run_one(seed: int) -> float:
        bus = Bus()
        rng = np.random.default_rng(seed)
        reflex = FlyNeuroReflex(bus, neuro_cfg=_cfg(), rng=rng)
        q = bus.subscribe("arousal.away")
        task = asyncio.create_task(reflex.run())
        try:
            await asyncio.sleep(0.02)
            bus.publish(Event(topic="idle.enter", payload={"seconds": 300.0}))
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            return ev.payload["seconds"]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    s1 = await run_one(42)
    s2 = await run_one(42)
    # idle_seconds at fire is driven by deterministic RNG => must agree to
    # within one tick (dt = 1/tick_hz = 0.01 s).
    assert math.isclose(s1, s2, abs_tol=0.01), (
        f"seeded runs diverged: s1={s1}, s2={s2}"
    )


async def test_neuro_fly_spike_topic_emitted() -> None:
    bus = Bus()
    rng = np.random.default_rng(99)
    reflex = FlyNeuroReflex(bus, neuro_cfg=_cfg(), rng=rng)
    spike_q = bus.subscribe("neuro.fly.spike")
    task = asyncio.create_task(reflex.run())
    try:
        await asyncio.sleep(0.02)
        bus.publish(Event(topic="idle.enter", payload={"seconds": 300.0}))
        ev = await asyncio.wait_for(spike_q.get(), timeout=1.0)
        assert ev.topic == "neuro.fly.spike"
        v = ev.payload["v"]
        noise = ev.payload["noise"]
        assert math.isfinite(v)
        assert math.isfinite(noise)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
