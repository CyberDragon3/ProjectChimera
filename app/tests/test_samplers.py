"""Tests for app.tier2_translation samplers.

All external I/O (mss screen grab, psutil, pynput) is patched so tests never
touch the real screen / cursor / system metrics.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.contracts import BioPolicy, MousePolicy
from app.event_bus import Snapshot, StimulusBus
from app import tier2_translation as t2


CFG = {
    "translation": {
        "ommatidia": {"grid": 8, "fps": 30, "region": None},
        "pressure": {"hz": 20, "cpu_weight": 0.6, "ram_weight": 0.4},
        "cursor": {"hz": 60},
    }
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeShot:
    """Emulates a mss ScreenShot object: ndarray-compatible + height/width."""

    def __init__(self, arr: np.ndarray) -> None:
        self._arr = arr
        self.height, self.width = arr.shape[0], arr.shape[1]

    def __array__(self, dtype=None):
        if dtype is None:
            return self._arr
        return self._arr.astype(dtype)


class _FakeSct:
    """Emulates the mss.mss() context-manager object."""

    def __init__(self, frame_provider) -> None:
        self._frame_provider = frame_provider
        self.monitors = [
            {"top": 0, "left": 0, "width": 200, "height": 200},
            {"top": 0, "left": 0, "width": 200, "height": 200},
        ]

    def __enter__(self) -> "_FakeSct":
        return self

    def __exit__(self, *a) -> None:
        return None

    def grab(self, _monitor):
        return _FakeShot(self._frame_provider())


def _make_fake_mss(frame_provider):
    fake = MagicMock()
    fake.return_value = _FakeSct(frame_provider)
    return fake


# ---------------------------------------------------------------------------
# 1. Ommatidia sampler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ommatidia_sampler_emits_frames_with_correct_shape() -> None:
    # alternate two distinct frames so diff is sometimes non-zero
    counter = {"n": 0}

    def provider() -> np.ndarray:
        counter["n"] += 1
        if counter["n"] % 2 == 0:
            arr = np.full((200, 200, 4), 200, dtype=np.uint8)
        else:
            arr = np.full((200, 200, 4), 50, dtype=np.uint8)
        return arr

    stim_bus = StimulusBus()
    stop_event = asyncio.Event()
    snapshot = Snapshot(policy=BioPolicy())

    frames: list = []

    async def drainer():
        while not stop_event.is_set() or not stim_bus.ommatidia.empty():
            try:
                f = await asyncio.wait_for(stim_bus.ommatidia.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            frames.append(f)

    with patch("mss.mss", _make_fake_mss(provider)):
        drain_task = asyncio.create_task(drainer())
        sampler_task = asyncio.create_task(
            t2.run_ommatidia_sampler(stim_bus, CFG, stop_event, snapshot)
        )
        await asyncio.sleep(0.5)
        stop_event.set()
        await asyncio.gather(sampler_task, drain_task)

    assert len(frames) >= 5, f"expected >=5 frames, got {len(frames)}"
    grid = CFG["translation"]["ommatidia"]["grid"]
    for f in frames:
        assert f.luminance.shape == (grid, grid)
        assert f.luminance.dtype == np.float32
        assert f.diff.shape == (grid, grid)
        assert f.diff.dtype == np.float32

    # first frame diff is all zero
    assert np.all(frames[0].diff == 0.0)
    # at least one later frame has a non-zero diff (provider alternates)
    assert any(np.any(f.diff != 0.0) for f in frames[1:])


# ---------------------------------------------------------------------------
# 2. Pressure sampler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pressure_sampler_fuses_cpu_ram() -> None:
    stim_bus = StimulusBus()
    stop_event = asyncio.Event()
    snapshot = Snapshot(policy=BioPolicy())

    fake_vm = MagicMock()
    fake_vm.return_value = MagicMock(percent=60.0)

    samples: list = []

    async def drainer():
        while not stop_event.is_set() or not stim_bus.pressure.empty():
            try:
                s = await asyncio.wait_for(stim_bus.pressure.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            samples.append(s)

    with patch("psutil.cpu_percent", return_value=50.0), \
         patch("psutil.virtual_memory", fake_vm):
        drain_task = asyncio.create_task(drainer())
        sampler_task = asyncio.create_task(
            t2.run_pressure_sampler(stim_bus, CFG, stop_event, snapshot)
        )
        await asyncio.sleep(0.5)
        stop_event.set()
        await asyncio.gather(sampler_task, drain_task)

    assert len(samples) >= 5, f"expected >=5 samples, got {len(samples)}"
    # pressure = 0.6*0.5 + 0.4*0.6 = 0.54
    for s in samples:
        assert abs(s.cpu - 0.5) < 1e-6
        assert abs(s.ram - 0.6) < 1e-6
        assert abs(s.pressure - 0.54) < 1e-6
    # first derivative is 0 (no prior sample); later derivatives near 0 as well
    assert samples[0].derivative == 0.0
    for s in samples[1:]:
        assert abs(s.derivative) < 1e-3


# ---------------------------------------------------------------------------
# 3. Cursor sampler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cursor_sampler_computes_velocity() -> None:
    stim_bus = StimulusBus()
    stop_event = asyncio.Event()
    snapshot = Snapshot(policy=BioPolicy())

    state = {"x": 0, "y": 0}

    class FakeController:
        @property
        def position(self):
            state["x"] += 10
            return (state["x"], state["y"])

    samples: list = []

    async def drainer():
        while not stop_event.is_set() or not stim_bus.cursor.empty():
            try:
                s = await asyncio.wait_for(stim_bus.cursor.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            samples.append(s)

    with patch("pynput.mouse.Controller", FakeController):
        drain_task = asyncio.create_task(drainer())
        sampler_task = asyncio.create_task(
            t2.run_cursor_sampler(stim_bus, CFG, stop_event, snapshot)
        )
        await asyncio.sleep(0.5)
        stop_event.set()
        await asyncio.gather(sampler_task, drain_task)

    assert len(samples) >= 10, f"expected >=10 samples, got {len(samples)}"
    # first sample has vx=0, subsequent samples have vx > 0 (x steps +10)
    assert samples[0].vx == 0.0
    assert samples[0].vy == 0.0
    assert any(s.vx > 0 for s in samples[1:])


# ---------------------------------------------------------------------------
# 4. compute_sugar
# ---------------------------------------------------------------------------

def test_compute_sugar_at_target_is_one() -> None:
    policy = BioPolicy(mouse=MousePolicy(track_target_xy=(100, 100)))
    val = t2.compute_sugar(policy, (100, 100), (1000, 1000))
    assert abs(val - 1.0) < 1e-9


def test_compute_sugar_at_two_sigma_below_threshold() -> None:
    policy = BioPolicy(mouse=MousePolicy(track_target_xy=(500, 500)))
    # sigma = max(1000,1000) * 0.15 = 150.  2*sigma = 300 px in one axis.
    val = t2.compute_sugar(policy, (500 + 300, 500), (1000, 1000))
    # exp(-(300^2)/(2*150^2)) = exp(-2) ~ 0.135
    assert val < 0.2


def test_compute_sugar_none_target_is_zero() -> None:
    policy = BioPolicy(mouse=MousePolicy(track_target_xy=None))
    val = t2.compute_sugar(policy, (100, 100), (1000, 1000))
    assert val == 0.0


# ---------------------------------------------------------------------------
# 5. Clean shutdown of run()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_clean_shutdown() -> None:
    stim_bus = StimulusBus()
    stop_event = asyncio.Event()
    snapshot = Snapshot(policy=BioPolicy())

    def provider() -> np.ndarray:
        return np.full((200, 200, 4), 128, dtype=np.uint8)

    fake_vm = MagicMock()
    fake_vm.return_value = MagicMock(percent=50.0)

    class FakeController:
        @property
        def position(self):
            return (0, 0)

    async def drain_all():
        while not stop_event.is_set():
            for q in (stim_bus.ommatidia, stim_bus.pressure, stim_bus.cursor):
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await asyncio.sleep(0.01)

    with patch("mss.mss", _make_fake_mss(provider)), \
         patch("psutil.cpu_percent", return_value=50.0), \
         patch("psutil.virtual_memory", fake_vm), \
         patch("pynput.mouse.Controller", FakeController):
        drain_task = asyncio.create_task(drain_all())
        run_task = asyncio.create_task(t2.run(stim_bus, CFG, stop_event, snapshot))
        # let the samplers spin up
        await asyncio.sleep(0.1)
        t_stop = time.perf_counter()
        stop_event.set()
        await asyncio.wait_for(run_task, timeout=1.0)
        elapsed_ms = (time.perf_counter() - t_stop) * 1000.0
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

    assert elapsed_ms < 200.0, f"run() took {elapsed_ms:.1f} ms to shut down"
