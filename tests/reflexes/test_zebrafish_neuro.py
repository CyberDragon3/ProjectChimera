"""Tests for the LIF-driven Zebrafish thermal governor."""

from __future__ import annotations

import asyncio
import time

import pytest

from chimera.bus import Bus
from chimera.config import NeuroCfg
from chimera.reflexes.zebrafish import ZebrafishNeuroReflex
from chimera.store import RingBuffer


def _fast_neuro_cfg(tick_hz: int = 200) -> NeuroCfg:
    """Tight-loop neuro config tuned so tests converge in well under 1 s."""
    return NeuroCfg(
        enabled=True,
        tick_hz=tick_hz,
        tau_m_ms=20.0,
        v_rest_mv=-65.0,
        v_reset_mv=-70.0,
        v_thresh_mv=-50.0,
        refractory_ms=2.0,
        noise_sigma_mv=0.0,
        zebrafish_input_gain=8.0,
    )


async def _run_briefly(coro_task: asyncio.Task[None], seconds: float) -> None:
    await asyncio.sleep(seconds)
    coro_task.cancel()
    await asyncio.gather(coro_task, return_exceptions=True)


async def test_ramp_fires_thermal_critical() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    now = time.monotonic()
    # 10 °C/sec ramp across 2 s of fake history (well above any sane slope).
    for i in range(21):
        buf.append(50.0 + 10.0 * (i / 10.0), ts=now + (i / 10.0))

    reflex = ZebrafishNeuroReflex(
        bus,
        buf,
        neuro_cfg=_fast_neuro_cfg(),
        critical_c=95.0,
        critical_clear_c=90.0,
        critical_samples=2,
        window_seconds=5.0,
    )
    q = bus.subscribe("thermal.critical")
    task = asyncio.create_task(reflex.run())
    try:
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev.payload["on"] is True
        assert ev.payload.get("cause") in {"lif_spike", "hard_floor"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_flat_temps_stay_silent() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    now = time.monotonic()
    for i in range(30):
        buf.append(50.0, ts=now + i)

    reflex = ZebrafishNeuroReflex(
        bus,
        buf,
        neuro_cfg=_fast_neuro_cfg(),
        critical_c=95.0,
        critical_clear_c=90.0,
        critical_samples=2,
        window_seconds=5.0,
    )
    crit_q = bus.subscribe("thermal.critical")
    spike_q = bus.subscribe("neuro.zebrafish.spike")
    task = asyncio.create_task(reflex.run())
    try:
        # Let the reflex churn ~200 ticks — nothing should fire.
        await asyncio.sleep(1.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert crit_q.empty(), "flat temperatures must not raise thermal.critical"
    assert spike_q.empty(), "flat temperatures must not spike the LIF neuron"


async def test_hard_floor_still_triggers() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    now = time.monotonic()
    # Flat 97 °C — zero slope, so the LIF path is silent.
    for i in range(5):
        buf.append(97.0, ts=now + i)

    reflex = ZebrafishNeuroReflex(
        bus,
        buf,
        neuro_cfg=_fast_neuro_cfg(),
        critical_c=95.0,
        critical_clear_c=90.0,
        critical_samples=2,
        window_seconds=5.0,
    )
    q = bus.subscribe("thermal.critical")
    task = asyncio.create_task(reflex.run())
    try:
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev.payload["on"] is True
        assert ev.payload.get("cause") == "hard_floor"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_clear_after_cooldown() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    now = time.monotonic()
    # Hot flat — guarantees hard-floor entry without needing a ramp.
    for i in range(5):
        buf.append(97.0, ts=now + i)

    reflex = ZebrafishNeuroReflex(
        bus,
        buf,
        neuro_cfg=_fast_neuro_cfg(),
        critical_c=95.0,
        critical_clear_c=90.0,
        critical_samples=2,
        window_seconds=5.0,
    )
    q = bus.subscribe("thermal.critical")
    task = asyncio.create_task(reflex.run())
    try:
        ev_on = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev_on.payload["on"] is True

        # Inject a cool tail: temperatures below the clear threshold.
        cool_t0 = now + 10.0
        for i in range(5):
            buf.append(85.0, ts=cool_t0 + i)

        # Expect an off event — either from hard-floor clear or slope cooldown.
        ev_off = await asyncio.wait_for(q.get(), timeout=1.0)
        assert ev_off.payload["on"] is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_neuro_spike_topic_emitted() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    now = time.monotonic()
    # Same aggressive ramp as test 1 — forces LIF to fire.
    for i in range(21):
        buf.append(50.0 + 10.0 * (i / 10.0), ts=now + (i / 10.0))

    reflex = ZebrafishNeuroReflex(
        bus,
        buf,
        neuro_cfg=_fast_neuro_cfg(),
        critical_c=200.0,  # keep hard floor out of the way for this test
        critical_clear_c=150.0,
        critical_samples=2,
        window_seconds=5.0,
    )
    q = bus.subscribe("neuro.zebrafish.spike")
    task = asyncio.create_task(reflex.run())
    try:
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert "v" in ev.payload
        assert "current" in ev.payload
        assert isinstance(ev.payload["v"], float)
        assert isinstance(ev.payload["current"], float)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# pytest-asyncio in auto mode — no decorators needed.
_ = pytest  # silence "unused import" warnings when pytest fixtures aren't referenced
