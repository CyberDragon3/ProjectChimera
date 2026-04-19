"""Tier 2 — Translation (Spatio-Temporal Mapping).

OWNER: Agent-Translation.

Converts digital system signals into biological stimuli for the three
animal connectome modules:
  * run_ommatidia_sampler — screen pixels -> NxN luminance grid (~30 Hz)
                            mimics Drosophila compound eye (fly module input)
  * run_pressure_sampler  — CPU+RAM -> fused somatosensory pressure (~20 Hz)
                            mimics C. elegans skin touch (worm module input)
  * run_cursor_sampler    — cursor position + velocity (~60 Hz)
                            mimics MICrONS mouse visual cortex input stream
  * compute_sugar         — 2D-Gaussian attractor field centered on policy
                            target, models chemical gradient for chemotaxis
  * run                   — asyncio.gather convenience for main.py

Contracts (do not change): see app/contracts.py.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any, Optional

import numpy as np

from .contracts import BioPolicy, CursorSample, OmmatidiaFrame, PressureSample
from .event_bus import Snapshot, StimulusBus, now_ns


# ---------------------------------------------------------------------------
# Ommatidia — screen luminance grid
# ---------------------------------------------------------------------------

def _bgra_to_luminance_grid(arr: np.ndarray, grid: int) -> np.ndarray:
    """Convert an HxWx4 BGRA uint8 array to a (grid, grid) float32 0..1 grid
    via Rec.601 luminance and mean-pooling."""
    # mss returns BGRA; arr[..., 0]=B, 1=G, 2=R
    b = arr[..., 0].astype(np.float32)
    g = arr[..., 1].astype(np.float32)
    r = arr[..., 2].astype(np.float32)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0  # (H, W) float32

    h, w = lum.shape
    # Crop to nearest multiple of grid so reshape works.
    ch = (h // grid) * grid
    cw = (w // grid) * grid
    if ch == 0 or cw == 0:
        # Fallback: resize via simple tiling
        return np.zeros((grid, grid), dtype=np.float32)
    lum = lum[:ch, :cw]
    bh = ch // grid
    bw = cw // grid
    # mean-pool: (grid, bh, grid, bw) -> mean over bh and bw
    pooled = lum.reshape(grid, bh, grid, bw).mean(axis=(1, 3))
    return pooled.astype(np.float32)


async def run_ommatidia_sampler(
    stim_bus: StimulusBus,
    cfg: dict[str, Any],
    stop_event: asyncio.Event,
    snapshot: Snapshot,
) -> None:
    import mss  # local import so tests can patch

    t_cfg = cfg["translation"]["ommatidia"]
    grid: int = int(t_cfg.get("grid", 32))
    fps: float = float(t_cfg.get("fps", 30))
    target_dt = 1.0 / max(1e-3, fps)
    region: Optional[dict[str, int]] = t_cfg.get("region")

    prev: Optional[np.ndarray] = None
    loop = asyncio.get_event_loop()

    with mss.mss() as sct:
        if region is None:
            monitors = sct.monitors
            # monitors[0] is the union of all monitors; [1] is typically primary
            mon = monitors[1] if len(monitors) > 1 else monitors[0]
        else:
            mon = region

        while not stop_event.is_set():
            start = loop.time()
            try:
                raw = sct.grab(mon)
                # raw.raw is bytes; raw.height, raw.width; use np.array(raw)
                arr = np.asarray(raw, dtype=np.uint8)
                if arr.ndim == 1:
                    # some mss stubs return flat — reshape
                    arr = arr.reshape(raw.height, raw.width, 4)
                elif arr.ndim == 3 and arr.shape[2] != 4:
                    # unusual channel layout — fall back
                    arr = arr[..., :4] if arr.shape[2] >= 4 else np.pad(
                        arr, ((0, 0), (0, 0), (0, 4 - arr.shape[2])), mode="constant"
                    )
            except Exception:  # noqa: BLE001
                await asyncio.sleep(target_dt)
                continue

            lum = _bgra_to_luminance_grid(arr, grid)
            if prev is None:
                diff = np.zeros_like(lum, dtype=np.float32)
            else:
                diff = (lum - prev).astype(np.float32)
            prev = lum

            frame = OmmatidiaFrame(t_ns=now_ns(), luminance=lum, diff=diff)
            await stim_bus.put_ommatidia(frame)
            snapshot.ommatidia = frame

            # update sugar concentration snapshot (cheap)
            if snapshot.policy is not None and snapshot.cursor is not None:
                try:
                    h = int(mon.get("height")) if isinstance(mon, dict) else int(mon["height"])
                    w = int(mon.get("width")) if isinstance(mon, dict) else int(mon["width"])
                    snapshot.sugar_concentration = compute_sugar(
                        snapshot.policy,
                        (snapshot.cursor.x, snapshot.cursor.y),
                        (w, h),
                    )
                except Exception:  # noqa: BLE001
                    pass

            elapsed = loop.time() - start
            await asyncio.sleep(max(0.0, target_dt - elapsed))


# ---------------------------------------------------------------------------
# Pressure — CPU+RAM fused somatosensory signal
# ---------------------------------------------------------------------------

async def run_pressure_sampler(
    stim_bus: StimulusBus,
    cfg: dict[str, Any],
    stop_event: asyncio.Event,
    snapshot: Snapshot,
) -> None:
    import psutil  # local import so tests can patch

    t_cfg = cfg["translation"]["pressure"]
    hz: float = float(t_cfg.get("hz", 20))
    target_dt = 1.0 / max(1e-3, hz)
    cpu_w: float = float(t_cfg.get("cpu_weight", 0.6))
    ram_w: float = float(t_cfg.get("ram_weight", 0.4))

    # warm up cpu_percent — first call is bogus (returns 0 or since-start).
    try:
        psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001
        pass

    prev_pressure: Optional[float] = None
    prev_t_ns: Optional[int] = None
    # EMA smooths psutil's sample-to-sample jitter so the worm's "sharp poke"
    # path fires on real bursts, not noise. tau chosen for ~200 ms response.
    ema_pressure: Optional[float] = None
    ema_derivative: float = 0.0
    EMA_ALPHA = 0.35
    loop = asyncio.get_event_loop()

    while not stop_event.is_set():
        start = loop.time()
        try:
            cpu_pct = float(psutil.cpu_percent(interval=None))
            ram_pct = float(psutil.virtual_memory().percent)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(target_dt)
            continue

        cpu = max(0.0, min(1.0, cpu_pct / 100.0))
        ram = max(0.0, min(1.0, ram_pct / 100.0))
        raw_pressure = max(0.0, min(1.0, cpu_w * cpu + ram_w * ram))
        ema_pressure = raw_pressure if ema_pressure is None else (
            EMA_ALPHA * raw_pressure + (1 - EMA_ALPHA) * ema_pressure
        )
        pressure = ema_pressure

        t_ns = now_ns()
        if prev_pressure is None or prev_t_ns is None:
            derivative = 0.0
        else:
            dt_s = max(1e-9, (t_ns - prev_t_ns) / 1e9)
            raw_d = (pressure - prev_pressure) / dt_s
            ema_derivative = EMA_ALPHA * raw_d + (1 - EMA_ALPHA) * ema_derivative
            derivative = ema_derivative

        prev_pressure = pressure
        prev_t_ns = t_ns

        sample = PressureSample(
            t_ns=t_ns, cpu=cpu, ram=ram, pressure=pressure, derivative=derivative
        )
        await stim_bus.put_pressure(sample)
        snapshot.pressure = sample

        elapsed = loop.time() - start
        await asyncio.sleep(max(0.0, target_dt - elapsed))


# ---------------------------------------------------------------------------
# Cursor — position + finite-difference velocity
# ---------------------------------------------------------------------------

async def run_cursor_sampler(
    stim_bus: StimulusBus,
    cfg: dict[str, Any],
    stop_event: asyncio.Event,
    snapshot: Snapshot,
) -> None:
    from pynput.mouse import Controller  # local import so tests can patch

    t_cfg = cfg["translation"]["cursor"]
    hz: float = float(t_cfg.get("hz", 60))
    target_dt = 1.0 / max(1e-3, hz)

    ctl = Controller()
    prev_x: Optional[int] = None
    prev_y: Optional[int] = None
    prev_t_ns: Optional[int] = None
    loop = asyncio.get_event_loop()

    while not stop_event.is_set():
        start = loop.time()
        try:
            pos = ctl.position
            x, y = int(pos[0]), int(pos[1])
        except Exception:  # noqa: BLE001
            await asyncio.sleep(target_dt)
            continue

        t_ns = now_ns()
        if prev_x is None or prev_y is None or prev_t_ns is None:
            vx, vy = 0.0, 0.0
        else:
            dt_s = max(1e-9, (t_ns - prev_t_ns) / 1e9)
            vx = (x - prev_x) / dt_s
            vy = (y - prev_y) / dt_s

        prev_x, prev_y, prev_t_ns = x, y, t_ns

        sample = CursorSample(t_ns=t_ns, x=x, y=y, vx=vx, vy=vy)
        await stim_bus.put_cursor(sample)
        snapshot.cursor = sample

        elapsed = loop.time() - start
        await asyncio.sleep(max(0.0, target_dt - elapsed))


# ---------------------------------------------------------------------------
# compute_sugar — 2D Gaussian attractor field centered on policy target
# ---------------------------------------------------------------------------

def compute_sugar(
    policy: BioPolicy,
    cursor_xy: tuple[int, int],
    screen_size: tuple[int, int],
) -> float:
    """Return the 'sugar concentration' at `cursor_xy` as a 2D Gaussian
    centered on `policy.mouse.track_target_xy` with sigma =
    max(screen_w, screen_h) * 0.15.  Returns 0.0 if target is None."""
    target = policy.mouse.track_target_xy
    if target is None:
        return 0.0
    tx, ty = int(target[0]), int(target[1])
    cx, cy = int(cursor_xy[0]), int(cursor_xy[1])
    w, h = int(screen_size[0]), int(screen_size[1])
    sigma = max(w, h) * 0.15
    if sigma <= 0:
        return 1.0 if (cx == tx and cy == ty) else 0.0
    dx = cx - tx
    dy = cy - ty
    r2 = dx * dx + dy * dy
    return float(math.exp(-r2 / (2.0 * sigma * sigma)))


# ---------------------------------------------------------------------------
# run — convenience gather
# ---------------------------------------------------------------------------

async def run(
    stim_bus: StimulusBus,
    cfg: dict[str, Any],
    stop_event: asyncio.Event,
    snapshot: Snapshot,
) -> None:
    await asyncio.gather(
        run_ommatidia_sampler(stim_bus, cfg, stop_event, snapshot),
        run_pressure_sampler(stim_bus, cfg, stop_event, snapshot),
        run_cursor_sampler(stim_bus, cfg, stop_event, snapshot),
    )
