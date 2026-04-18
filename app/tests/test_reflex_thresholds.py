"""Tier-3 reflex threshold tests.

We never touch real psutil/mss/pynput here — we manually hand-craft stimuli
and push them onto the StimulusBus queues, then assert that the connectome
publishes (or does NOT publish) an InterruptEvent on the InterruptBus.
"""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from app.contracts import (
    BioPolicy,
    CursorSample,
    FlyPolicy,
    MousePolicy,
    OmmatidiaFrame,
    PressureSample,
    WormPolicy,
)
from app.event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus
from app.tier3_reflex import fly as fly_mod
from app.tier3_reflex import mouse as mouse_mod
from app.tier3_reflex import worm as worm_mod

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _outward_diff(grid: int = 8, amplitude: float = 1.0) -> np.ndarray:
    """Build a `diff` field whose sign matches the dominant radial component
    at each cell — i.e. a textbook "outward looming" pattern. Scoring this
    with FlyConnectome gives ~amplitude * (1 - 1/grid**2)."""
    ii, jj = np.indices((grid, grid)).astype(np.float32)
    cy = (grid - 1) / 2.0
    cx = (grid - 1) / 2.0
    ry = ii - cy
    rx = jj - cx
    use_y = np.abs(ry) >= np.abs(rx)
    sign = np.where(use_y, np.sign(ry), np.sign(rx)).astype(np.float32)
    return sign * float(amplitude)


def _frame(diff: np.ndarray, t_ns: int = 0) -> OmmatidiaFrame:
    grid = diff.shape[0]
    return OmmatidiaFrame(
        t_ns=t_ns,
        luminance=np.zeros((grid, grid), dtype=np.float32),
        diff=diff.astype(np.float32),
    )


def _make_bus_policy(policy: BioPolicy):
    stim = StimulusBus(maxsize=32)
    ibus = InterruptBus()
    store = PolicyStore(policy)
    snap = Snapshot(policy=policy)
    stop = asyncio.Event()
    return stim, ibus, store, snap, stop


async def _wait_for_event(ibus: InterruptBus, timeout: float = 0.4):
    try:
        return await asyncio.wait_for(ibus.main.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


async def _drain(ibus: InterruptBus, until: float = 0.2) -> list:
    """Collect any events published within `until` seconds (no event = []).
    Useful for asserting negative cases and counting fires."""
    events = []
    deadline = time.perf_counter() + until
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        try:
            ev = await asyncio.wait_for(ibus.main.get(), timeout=remaining)
            events.append(ev)
        except asyncio.TimeoutError:
            break
    return events


async def _stop(stop: asyncio.Event, task: asyncio.Task) -> None:
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=0.3)
    except asyncio.TimeoutError:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task


# ---------------------------------------------------------------------------
# Fly
# ---------------------------------------------------------------------------


async def test_fly_fires_on_outward_divergence():
    policy = BioPolicy(fly=FlyPolicy(sensitivity=0.5, looming_threshold=0.35))
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(fly_mod.run(stim, ibus, store, snap, stop))
    try:
        await stim.put_ommatidia(_frame(_outward_diff(8, 1.0), t_ns=1))
        ev = await _wait_for_event(ibus, timeout=0.5)
        assert ev is not None, "fly should fire on strong outward divergence"
        assert ev.module == "fly"
        assert ev.kind == "looming"
        assert ev.payload["flow"] > 0.35
        assert len(snap.fly_spikes) == 1
    finally:
        await _stop(stop, task)


async def test_fly_does_not_fire_on_zero_diff():
    policy = BioPolicy(fly=FlyPolicy(sensitivity=0.5, looming_threshold=0.35))
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(fly_mod.run(stim, ibus, store, snap, stop))
    try:
        await stim.put_ommatidia(
            _frame(np.zeros((8, 8), dtype=np.float32), t_ns=1)
        )
        events = await _drain(ibus, until=0.2)
        assert events == []
        assert len(snap.fly_spikes) == 0
    finally:
        await _stop(stop, task)


async def test_fly_respects_refractory():
    policy = BioPolicy(fly=FlyPolicy(sensitivity=0.5, looming_threshold=0.35))
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(fly_mod.run(stim, ibus, store, snap, stop))
    try:
        # Fire twice in rapid succession — second must be swallowed by the
        # 300 ms refractory.
        await stim.put_ommatidia(_frame(_outward_diff(8, 1.0), t_ns=1))
        await asyncio.sleep(0.02)
        await stim.put_ommatidia(_frame(_outward_diff(8, 1.0), t_ns=2))
        events = await _drain(ibus, until=0.25)
        assert len(events) == 1, f"expected 1 fire, got {len(events)}"
        assert len(snap.fly_spikes) == 1
    finally:
        await _stop(stop, task)


async def test_fly_sensitivity_modulates_threshold():
    # Build a mild stimulus whose score sits between the effective thresholds
    # for low and high sensitivity. amplitude=0.35 gives score ~0.34; with
    # looming_threshold=0.35:
    #   sensitivity=0.0 => eff = 0.35*(1.4)        = 0.490  (no fire)
    #   sensitivity=1.0 => eff = 0.35*(1-0.8+0.4)  = 0.210  (fires)
    diff = _outward_diff(8, 0.35)

    # Low sensitivity: should NOT fire.
    low = BioPolicy(fly=FlyPolicy(sensitivity=0.0, looming_threshold=0.35))
    stim, ibus, store, snap, stop = _make_bus_policy(low)
    task = asyncio.create_task(fly_mod.run(stim, ibus, store, snap, stop))
    try:
        await stim.put_ommatidia(_frame(diff, t_ns=1))
        events = await _drain(ibus, until=0.2)
        assert events == [], "low sensitivity should not fire on mild stim"
    finally:
        await _stop(stop, task)

    # High sensitivity: should fire on the same stimulus.
    high = BioPolicy(fly=FlyPolicy(sensitivity=1.0, looming_threshold=0.35))
    stim, ibus, store, snap, stop = _make_bus_policy(high)
    task = asyncio.create_task(fly_mod.run(stim, ibus, store, snap, stop))
    try:
        await stim.put_ommatidia(_frame(diff, t_ns=1))
        ev = await _wait_for_event(ibus, timeout=0.4)
        assert ev is not None, "high sensitivity should fire on mild stim"
    finally:
        await _stop(stop, task)


# ---------------------------------------------------------------------------
# Worm
# ---------------------------------------------------------------------------


async def test_worm_sustained_pain_fires_once():
    policy = BioPolicy(
        worm=WormPolicy(
            cpu_pain_threshold=0.85,
            ram_pain_threshold=0.90,
            poke_derivative=0.25,
            dwell_ms=100,
        )
    )
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(worm_mod.run(stim, ibus, store, snap, stop))
    try:
        base = 1_000_000_000  # 1 s in ns — arbitrary
        # Sample at t=0 crosses; second at t=120ms exceeds dwell=100ms.
        samples = [
            PressureSample(t_ns=base, cpu=0.95, ram=0.5, pressure=0.9,
                           derivative=0.0),
            PressureSample(t_ns=base + 120_000_000, cpu=0.95, ram=0.5,
                           pressure=0.9, derivative=0.0),
            PressureSample(t_ns=base + 180_000_000, cpu=0.95, ram=0.5,
                           pressure=0.9, derivative=0.0),
        ]
        for s in samples:
            await stim.put_pressure(s)
        events = await _drain(ibus, until=0.35)
        # Second sample should fire (sustained). Third arrives within 500 ms
        # refractory, so only one event.
        assert len(events) == 1, f"expected exactly 1 fire, got {len(events)}"
        assert events[0].payload["path"] == "sustained"
        assert events[0].payload["cpu"] == pytest.approx(0.95)
    finally:
        await _stop(stop, task)


async def test_worm_sharp_poke_fires_immediately():
    policy = BioPolicy(
        worm=WormPolicy(
            cpu_pain_threshold=0.85,
            ram_pain_threshold=0.90,
            poke_derivative=0.25,
            dwell_ms=800,
        )
    )
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(worm_mod.run(stim, ibus, store, snap, stop))
    try:
        await stim.put_pressure(
            PressureSample(
                t_ns=1, cpu=0.2, ram=0.2, pressure=0.3, derivative=0.9
            )
        )
        ev = await _wait_for_event(ibus, timeout=0.4)
        assert ev is not None, "worm should fire on sharp poke"
        assert ev.payload["path"] == "poke"
        assert ev.payload["derivative"] == pytest.approx(0.9)
    finally:
        await _stop(stop, task)


async def test_worm_idle_stream_does_not_fire():
    policy = BioPolicy(
        worm=WormPolicy(
            cpu_pain_threshold=0.85,
            ram_pain_threshold=0.90,
            poke_derivative=0.25,
            dwell_ms=100,
        )
    )
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(worm_mod.run(stim, ibus, store, snap, stop))
    try:
        base = 1_000_000_000
        for i in range(5):
            await stim.put_pressure(
                PressureSample(
                    t_ns=base + i * 50_000_000,
                    cpu=0.2, ram=0.2, pressure=0.2, derivative=0.01,
                )
            )
        events = await _drain(ibus, until=0.25)
        assert events == []
        assert len(snap.worm_spikes) == 0
    finally:
        await _stop(stop, task)


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------


async def test_mouse_error_spike_requires_consecutive_frames():
    policy = BioPolicy(
        mouse=MousePolicy(error_threshold=50.0, consecutive_frames=3)
    )
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(mouse_mod.run(stim, ibus, store, snap, stop))
    try:
        dt_ns = 10_000_000  # 10 ms
        # Prime predictor with steady motion: 10 px/step at 10 ms per sample
        # => vx = 1000 px/s.
        samples = [CursorSample(t_ns=i * dt_ns, x=i * 10, y=0, vx=1000.0, vy=0.0)
                   for i in range(4)]
        for s in samples:
            await stim.put_cursor(s)

        # No fire yet — steady motion has near-zero prediction error.
        events = await _drain(ibus, until=0.1)
        assert events == [], "smooth motion should not fire"

        # Now inject a huge jump repeatedly. We keep vx=1000 on every jump
        # sample so the predictor consistently expects +10 px/step — the
        # actual position is far beyond that, so every jump produces a large
        # prediction error. `consecutive_frames`=3 means 3 such frames needed.
        big = 5_000  # way outside threshold
        jump_samples = [
            CursorSample(t_ns=(4 + i) * dt_ns, x=big + i * 2000, y=0,
                         vx=1000.0, vy=0.0)
            for i in range(3)
        ]
        # Push first jump — error is large but streak = 1, no fire.
        await stim.put_cursor(jump_samples[0])
        ev = await _wait_for_event(ibus, timeout=0.1)
        assert ev is None, "should not fire after only 1 high-error frame"

        # Push second jump — streak = 2, still no fire.
        await stim.put_cursor(jump_samples[1])
        ev = await _wait_for_event(ibus, timeout=0.1)
        assert ev is None, "should not fire after only 2 high-error frames"

        # Third jump — streak = 3, should fire exactly once.
        await stim.put_cursor(jump_samples[2])
        ev = await _wait_for_event(ibus, timeout=0.4)
        assert ev is not None, "should fire on 3rd consecutive high-error frame"
        assert ev.module == "mouse"
        assert ev.kind == "error_spike"
        assert ev.payload["error"] > 50.0
    finally:
        await _stop(stop, task)


async def test_mouse_smooth_trajectory_does_not_fire():
    policy = BioPolicy(
        mouse=MousePolicy(error_threshold=50.0, consecutive_frames=3)
    )
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    task = asyncio.create_task(mouse_mod.run(stim, ibus, store, snap, stop))
    try:
        dt_ns = 10_000_000
        samples = [
            CursorSample(t_ns=i * dt_ns, x=i * 5, y=i * 3,
                         vx=500.0, vy=300.0)
            for i in range(10)
        ]
        for s in samples:
            await stim.put_cursor(s)
        events = await _drain(ibus, until=0.25)
        assert events == [], f"smooth linear trajectory fired: {events}"
        assert len(snap.mouse_spikes) == 0
    finally:
        await _stop(stop, task)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_all_three_exit_on_stop_event():
    policy = BioPolicy()
    stim, ibus, store, snap, stop = _make_bus_policy(policy)
    tasks = [
        asyncio.create_task(fly_mod.run(stim, ibus, store, snap, stop)),
        asyncio.create_task(worm_mod.run(stim, ibus, store, snap, stop)),
        asyncio.create_task(mouse_mod.run(stim, ibus, store, snap, stop)),
    ]
    # Let them spin up and enter their queue-wait.
    await asyncio.sleep(0.05)

    t0 = time.perf_counter()
    stop.set()
    done, pending = await asyncio.wait(tasks, timeout=0.2)
    elapsed = time.perf_counter() - t0

    for t in pending:
        t.cancel()
    assert not pending, (
        f"{len(pending)} connectome task(s) did not exit in 200 ms "
        f"(elapsed={elapsed:.3f}s)"
    )
    # Surface any exceptions.
    for t in done:
        exc = t.exception()
        assert exc is None, f"task raised: {exc!r}"
