"""Drosophila T4/T5 looming detector.

Idea: a T4/T5-style motion-sensitive population fires on outward optic flow
(an expanding stimulus across the visual field = something rushing at you).

We don't actually reconstruct flow; we use the signed `diff` grid as a cheap
proxy. For each cell we ask: is the diff aligned with the radial vector from
the image centre? If yes, accumulate `|diff|`. Normalised by grid*grid this
gives a dimensionless "outward flow" score that's large when the whole field
is expanding and small for zero-diff or pure translation.

Fire when `score > looming_threshold * (1.0 - 0.8*sensitivity + 0.4)` — higher
sensitivity => lower effective threshold.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import numpy as np

from ..contracts import BioPolicy, InterruptEvent, OmmatidiaFrame
from ..event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus
from .base import Connectome


class FlyConnectome(Connectome):
    module = "fly"
    refractory_s = 0.300  # 300 ms

    def __init__(self) -> None:
        self._radial_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def _radial(self, grid: int) -> tuple[np.ndarray, np.ndarray]:
        cached = self._radial_cache.get(grid)
        if cached is not None:
            return cached
        # Unit radial vector from centre to each cell. Centre cell gets (0,0).
        ii, jj = np.indices((grid, grid)).astype(np.float32)
        cy = (grid - 1) / 2.0
        cx = (grid - 1) / 2.0
        ry = ii - cy
        rx = jj - cx
        mag = np.sqrt(ry * ry + rx * rx)
        mag[mag == 0] = 1.0  # avoid div-by-zero at centre
        ry /= mag
        rx /= mag
        self._radial_cache[grid] = (ry, rx)
        return ry, rx

    def _flow_score(self, diff: np.ndarray) -> float:
        if diff.ndim != 2 or diff.size == 0:
            return 0.0
        grid = diff.shape[0]
        ry, rx = self._radial(grid)
        # Sign of the diff "projected" onto radius: for a scalar diff we can't
        # truly project — but we can approximate "outward" as diff sign ==
        # sign of the dominant radial component at that cell. Equivalently:
        # |diff| counts toward the score when diff * (sign of max(|ry|,|rx|))
        # > 0.
        # Simpler & robust approximation: treat positive diff as outward for
        # cells on the positive-radius side, negative diff as outward on the
        # negative-radius side — i.e. diff aligned with radial vector.
        # Use |ry| vs |rx| to pick which axis to test per cell.
        use_y = np.abs(ry) >= np.abs(rx)
        radial_sign = np.where(use_y, np.sign(ry), np.sign(rx))
        diff_sign = np.sign(diff)
        aligned = (diff_sign * radial_sign) > 0
        score = float(np.sum(np.abs(diff) * aligned)) / float(diff.size)
        return score

    @staticmethod
    def _effective_threshold(policy: BioPolicy) -> float:
        s = max(0.0, min(1.0, float(policy.fly.sensitivity)))
        return float(policy.fly.looming_threshold) * (1.0 - 0.8 * s + 0.4)

    async def _get_stimulus(
        self, stim_bus: StimulusBus
    ) -> Optional[OmmatidiaFrame]:
        try:
            return await asyncio.wait_for(stim_bus.ommatidia.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    async def _process(
        self, stim: OmmatidiaFrame, policy: BioPolicy
    ) -> Optional[InterruptEvent]:
        score = self._flow_score(stim.diff)
        thr = self._effective_threshold(policy)
        if score > thr:
            return InterruptEvent(
                module="fly",
                kind="looming",
                payload={"flow": score, "threshold": thr},
            )
        return None


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
