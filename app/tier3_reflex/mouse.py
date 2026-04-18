"""Mouse visual-cortex predictive-error spike.

Tiny constant-velocity predictor over incoming cursor samples. On each sample:
  predicted = last + velocity * dt
  error = euclidean(predicted, actual)

Fire when `error > mouse.error_threshold` for `mouse.consecutive_frames`
consecutive samples. 200 ms refractory.

Velocity is updated from the sample's own (vx, vy) if present, else estimated
from (actual - last) / dt.
"""
from __future__ import annotations

import asyncio
import math
from typing import Optional

import numpy as np

from ..contracts import BioPolicy, CursorSample, InterruptEvent
from ..event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus
from .base import Connectome


class MouseConnectome(Connectome):
    module = "mouse"
    refractory_s = 0.200  # 200 ms

    def __init__(self) -> None:
        self._last: Optional[CursorSample] = None
        self._vx: float = 0.0
        self._vy: float = 0.0
        self._streak: int = 0

    async def _get_stimulus(
        self, stim_bus: StimulusBus
    ) -> Optional[CursorSample]:
        try:
            return await asyncio.wait_for(stim_bus.cursor.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    async def _process(
        self, stim: CursorSample, policy: BioPolicy
    ) -> Optional[InterruptEvent]:
        last = self._last
        if last is None:
            # Prime the predictor; cannot yet compute a meaningful error.
            self._last = stim
            self._vx = float(stim.vx or 0.0)
            self._vy = float(stim.vy or 0.0)
            self._streak = 0
            return None

        dt_ns = max(1, int(stim.t_ns) - int(last.t_ns))
        dt = dt_ns / 1_000_000_000.0

        # Predict current position from last position + last known velocity.
        px = last.x + self._vx * dt
        py = last.y + self._vy * dt

        ex = float(stim.x) - px
        ey = float(stim.y) - py
        err = math.hypot(ex, ey)

        # Update velocity for the next step. Prefer sample-provided vx/vy when
        # non-zero (matches what the producer says); otherwise estimate.
        if stim.vx or stim.vy:
            self._vx = float(stim.vx)
            self._vy = float(stim.vy)
        elif dt > 0:
            self._vx = (float(stim.x) - float(last.x)) / dt
            self._vy = (float(stim.y) - float(last.y)) / dt
        self._last = stim

        thr = float(policy.mouse.error_threshold)
        needed = max(1, int(policy.mouse.consecutive_frames))

        if err > thr:
            self._streak += 1
            if self._streak >= needed:
                self._streak = 0
                return InterruptEvent(
                    module="mouse",
                    kind="error_spike",
                    payload={
                        "error": err,
                        "predicted": (float(px), float(py)),
                        "actual": (float(stim.x), float(stim.y)),
                    },
                )
        else:
            self._streak = 0

        return None


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
