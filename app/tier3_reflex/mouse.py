"""Mouse visual-cortex predictive error — spiking neural implementation.

A constant-velocity predictor still runs (so we have a clean "surprise"
signal), but the decision to fire is now produced by a small SNN whose
input is a short trajectory of (Δx, Δy, speed, error-magnitude) tuples.
Over time the net learns the user's characteristic cursor dynamics — so a
jerky flick that *you* make a thousand times a day stops being
"surprising", while a genuine predictive miss (dialog pops, cursor snaps,
drag-and-drop loses grip) keeps firing.

Learning signal:
  * After a fire, if the cursor quickly settles (error falls well below the
    threshold) → +reward.
  * If cursor continues to be erratic (error stays above threshold) →
    −reward; the brain was too twitchy for this user.
"""
from __future__ import annotations

import asyncio
import math
from collections import deque
from typing import Deque, Optional

import numpy as np

from ..contracts import BioPolicy, CursorSample, InterruptEvent
from ..event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus
from .base import Connectome
from .neural import BrainConfig, SpikingBrain


HISTORY_STEPS = 8
N_FEATURES_PER_STEP = 4          # dx/thr, dy/thr, speed_norm, err/thr
FEEDBACK_WINDOW_S = 0.8


class MouseConnectome(Connectome):
    module = "mouse"
    refractory_s = 0.200

    def __init__(self) -> None:
        self._last: Optional[CursorSample] = None
        self._vx: float = 0.0
        self._vy: float = 0.0
        self._streak: int = 0
        self._history: Deque[tuple[float, float, float, float]] = deque(
            maxlen=HISTORY_STEPS
        )
        self.brain = SpikingBrain(
            name="mouse",
            cfg=BrainConfig(
                n_in=HISTORY_STEPS * N_FEATURES_PER_STEP,
                n_hidden=32,
                target_hidden_rate_hz=5.0,
                target_readout_rate_hz=0.5,
            ),
        )
        self._pending_fb: Deque[tuple[int, float]] = deque(maxlen=16)
        self._last_stim_ns: int = 0

    async def _get_stimulus(self, stim_bus: StimulusBus) -> Optional[CursorSample]:
        try:
            return await asyncio.wait_for(stim_bus.cursor.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    def _encode(self) -> np.ndarray:
        if not self._history:
            return np.zeros(self.brain.cfg.n_in, dtype=np.float32)
        rows = list(self._history)
        while len(rows) < HISTORY_STEPS:
            rows.insert(0, rows[0])
        arr = np.asarray(rows, dtype=np.float32).ravel()
        return np.clip(arr, 0.0, 1.0)

    async def _process(self, stim: CursorSample, policy: BioPolicy) -> Optional[InterruptEvent]:
        t_ns = int(stim.t_ns)
        last = self._last
        if last is None:
            self._last = stim
            self._vx = float(stim.vx or 0.0)
            self._vy = float(stim.vy or 0.0)
            self._streak = 0
            self._last_stim_ns = t_ns
            return None

        dt_ns = max(1, t_ns - int(last.t_ns))
        dt = dt_ns / 1e9

        px = last.x + self._vx * dt
        py = last.y + self._vy * dt
        ex = float(stim.x) - px
        ey = float(stim.y) - py
        err = math.hypot(ex, ey)

        if stim.vx or stim.vy:
            self._vx, self._vy = float(stim.vx), float(stim.vy)
        elif dt > 0:
            self._vx = (float(stim.x) - float(last.x)) / dt
            self._vy = (float(stim.y) - float(last.y)) / dt
        self._last = stim

        thr = float(policy.mouse.error_threshold)
        thr_safe = max(thr, 1.0)
        speed = math.hypot(self._vx, self._vy)
        # Normalised features in [0,1]-ish.
        dx_n = min(abs(ex) / thr_safe, 1.0)
        dy_n = min(abs(ey) / thr_safe, 1.0)
        spd_n = min(speed / 2000.0, 1.0)
        err_n = min(err / thr_safe, 1.0)
        self._history.append((dx_n, dy_n, spd_n, err_n))

        step_dt = ((t_ns - self._last_stim_ns) / 1e9) if self._last_stim_ns else dt
        self._last_stim_ns = t_ns
        feat = self._encode()
        # Inhibitory gating: low error_threshold = LLM asked for hair-trigger
        # tracking → sensitise; high threshold = "let big jumps happen" → numb.
        gate = max(0.3, min(3.0, thr_safe / 120.0))
        fired, v_o = self.brain.step(feat, step_dt, gate=gate)
        self._drain_feedback(t_ns, err, thr_safe)

        # Heuristic safety net retained.
        needed = max(1, int(policy.mouse.consecutive_frames))
        hit = err > thr
        if hit:
            self._streak += 1
        else:
            self._streak = 0
        heuristic_fire = hit and self._streak >= needed
        if heuristic_fire:
            self._streak = 0

        if fired or heuristic_fire:
            self._pending_fb.append((t_ns, err))
            return InterruptEvent(
                module="mouse", kind="error_spike",
                payload={
                    "error": err,
                    "predicted": (float(px), float(py)),
                    "actual": (float(stim.x), float(stim.y)),
                    "brain_fired": bool(fired),
                    "heuristic_fired": bool(heuristic_fire),
                    "readout_v": round(v_o, 3),
                },
            )
        return None

    def _drain_feedback(self, t_ns: int, err_now: float, thr_safe: float) -> None:
        window_ns = int(FEEDBACK_WINDOW_S * 1e9)
        while self._pending_fb and (t_ns - self._pending_fb[0][0]) >= window_ns:
            _, err_at_fire = self._pending_fb.popleft()
            if err_at_fire <= 0:
                continue
            # Settled well under the threshold within the window → good.
            if err_now < 0.4 * thr_safe and err_now < 0.6 * err_at_fire:
                self.brain.deliver_reward(+1.0)
            # Still over threshold → probably an idiosyncratic user motion.
            elif err_now > thr_safe:
                self.brain.deliver_reward(-0.5)


async def run(
    stim_bus: StimulusBus,
    interrupt_bus: InterruptBus,
    policy_store: PolicyStore,
    snapshot: Snapshot,
    stop_event: asyncio.Event,
) -> None:
    await MouseConnectome().run(
        stim_bus, interrupt_bus, policy_store, snapshot, stop_event
    )
