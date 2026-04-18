"""Drosophila T4/T5 looming detector — spiking neural implementation.

Input encoding (ommatidia → rate code):
  * The signed diff grid is split into outward-aligned magnitude and
    non-outward residual per cell. That gives an interpretable 2-channel
    "looming vs other motion" feature that the SNN can re-weight as it
    learns what the user's screen looks like during a real threat (fast
    expanding window, progress bar, video dialog) vs ambient shimmer.
  * Values are clamped to [0, 1] — the `step()` call draws Bernoulli input
    spikes from them.

Learning signal (R-STDP):
  * If a looming fire happens and the outward-flow drops within ~1.2 s, the
    stimulus actually went away (closed the window, dismissed the dialog)
    → +reward: "that was a real looming, keep firing on shapes like this".
  * If the flow score is still just as loud 1.2 s later, the user did not
    react → the fire was probably a false alarm → −reward.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Deque, Optional

import numpy as np

from ..contracts import BioPolicy, InterruptEvent, OmmatidiaFrame
from ..event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus, now_ns
from .base import Connectome
from .neural import BrainConfig, SpikingBrain


GRID_DEFAULT = 32          # matches config.yaml translation.ommatidia.grid
FEEDBACK_WINDOW_S = 1.2


class FlyConnectome(Connectome):
    module = "fly"
    refractory_s = 0.300

    def __init__(self, grid: int = GRID_DEFAULT) -> None:
        self.grid = grid
        self._radial_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        # 2 channels × grid × grid input features.
        n_in = 2 * grid * grid
        self.brain = SpikingBrain(
            name="fly",
            cfg=BrainConfig(
                n_in=n_in, n_hidden=48,
                target_hidden_rate_hz=6.0,
                target_readout_rate_hz=0.8,
            ),
        )
        self._last_stim_ns: int = 0
        # Queue of (fire_t_ns, flow_at_fire) awaiting reward evaluation.
        self._pending_fb: Deque[tuple[int, float]] = deque(maxlen=16)

    # ---- input encoding ------------------------------------------------

    def _radial(self, grid: int) -> tuple[np.ndarray, np.ndarray]:
        cached = self._radial_cache.get(grid)
        if cached is not None:
            return cached
        ii, jj = np.indices((grid, grid)).astype(np.float32)
        cy = cx = (grid - 1) / 2.0
        ry, rx = ii - cy, jj - cx
        mag = np.sqrt(ry * ry + rx * rx)
        mag[mag == 0] = 1.0
        ry /= mag; rx /= mag
        self._radial_cache[grid] = (ry, rx)
        return ry, rx

    def _encode(self, diff: np.ndarray) -> tuple[np.ndarray, float]:
        """Return (feature_vector, scalar_flow_score)."""
        if diff.ndim != 2 or diff.size == 0:
            return np.zeros(self.brain.cfg.n_in, dtype=np.float32), 0.0
        grid = diff.shape[0]
        if grid != self.grid:
            # Stimulus grid changed size — resize net lazily would be heavy.
            # Pad/crop to the net's configured grid instead.
            diff = _resize_grid(diff, self.grid)
            grid = self.grid
        ry, rx = self._radial(grid)
        use_y = np.abs(ry) >= np.abs(rx)
        radial_sign = np.where(use_y, np.sign(ry), np.sign(rx))
        diff_sign = np.sign(diff)
        aligned = (diff_sign * radial_sign) > 0
        mag = np.abs(diff).astype(np.float32)
        outward = np.where(aligned, mag, 0.0)
        other = np.where(aligned, 0.0, mag)
        # Diff is per-frame luminance change (roughly [-1, 1]) — just clip,
        # don't per-frame-normalise, or a weak stimulus would look as loud
        # as a strong one to the SNN.
        feat = np.concatenate([outward.ravel(), other.ravel()])
        feat = np.clip(feat, 0.0, 1.0).astype(np.float32)
        score = float(outward.sum()) / float(diff.size)
        return feat, score

    # ---- stimulus pump -------------------------------------------------

    async def _get_stimulus(self, stim_bus: StimulusBus) -> Optional[OmmatidiaFrame]:
        try:
            return await asyncio.wait_for(stim_bus.ommatidia.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    @staticmethod
    def _effective_threshold(policy: BioPolicy) -> float:
        s = max(0.0, min(1.0, float(policy.fly.sensitivity)))
        return float(policy.fly.looming_threshold) * (1.0 - 0.8 * s + 0.4)

    async def _process(self, stim: OmmatidiaFrame, policy: BioPolicy) -> Optional[InterruptEvent]:
        t_ns = int(stim.t_ns)
        dt = ((t_ns - self._last_stim_ns) / 1e9) if self._last_stim_ns else 0.03
        self._last_stim_ns = t_ns

        feat, flow = self._encode(stim.diff)

        # Process delayed reward signals from prior fires.
        self._drain_feedback(t_ns, flow)

        # LLM-driven inhibitory gating: high sensitivity → lower gate →
        # the SNN fires on weaker evidence. Mirrors the heuristic's
        # effective-threshold shaping so the executive's "pay attention to
        # progress bars" command actually reaches the spiking brain.
        sens = max(0.0, min(1.0, float(policy.fly.sensitivity)))
        gate = 1.0 - 0.6 * sens + 0.3           # s=0 → 1.3, s=1 → 0.7
        fired, v_o = self.brain.step(feat, dt, gate=gate)

        # Safety net: the SNN starts naive, so back it with the classic
        # heuristic. Either path can fire — we want the reflex to work on
        # day 1 even before any learning has happened. R-STDP then teaches
        # the SNN which input shapes actually matter.
        thr = self._effective_threshold(policy)
        heuristic_fire = flow > thr

        if fired or heuristic_fire:
            self._pending_fb.append((t_ns, flow))
            return InterruptEvent(
                module="fly", kind="looming",
                payload={
                    "flow": flow, "threshold": thr,
                    "brain_fired": bool(fired),
                    "heuristic_fired": bool(heuristic_fire),
                    "readout_v": round(v_o, 3),
                },
            )
        return None

    def _drain_feedback(self, t_ns: int, current_flow: float) -> None:
        window_ns = int(FEEDBACK_WINDOW_S * 1e9)
        while self._pending_fb and (t_ns - self._pending_fb[0][0]) >= window_ns:
            _, flow_at_fire = self._pending_fb.popleft()
            if flow_at_fire <= 1e-6:
                continue
            ratio = current_flow / flow_at_fire
            # Flow dropped ≥50% → stimulus truly went away → good fire.
            # Still ≥90% of the way up → user didn't react at all → likely bad.
            if ratio < 0.5:
                self.brain.deliver_reward(+1.0)
            elif ratio > 0.9:
                self.brain.deliver_reward(-0.6)
            # Middle band: uncertain, no reward applied.


def _resize_grid(a: np.ndarray, g: int) -> np.ndarray:
    """Nearest-neighbour resize of a 2D grid to (g, g) — avoids a scipy dep."""
    h, w = a.shape
    yi = (np.linspace(0, h - 1, g)).astype(np.int64)
    xi = (np.linspace(0, w - 1, g)).astype(np.int64)
    return a[yi][:, xi]


async def run(
    stim_bus: StimulusBus,
    interrupt_bus: InterruptBus,
    policy_store: PolicyStore,
    snapshot: Snapshot,
    stop_event: asyncio.Event,
) -> None:
    await FlyConnectome().run(
        stim_bus, interrupt_bus, policy_store, snapshot, stop_event
    )
