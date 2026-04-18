"""C. elegans AVA recoil.

AVA is the command interneuron that drives backward crawl on noxious stimulus.
Here we model two fire paths:

1. Sustained pain: CPU pressure stays above `cpu_pain_threshold` for at least
   `dwell_ms` ms. RAM pressure above `ram_pain_threshold` independently also
   satisfies "sustained pain" (so a RAM-only leak can trigger too).
2. Sharp poke: `derivative` (d(pressure)/dt) exceeds `poke_derivative`.

500 ms refractory.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..contracts import BioPolicy, InterruptEvent, PressureSample
from ..event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus
from .base import Connectome


class WormConnectome(Connectome):
    module = "worm"
    refractory_s = 0.500  # 500 ms

    def __init__(self) -> None:
        # Stimulus ns at which CPU/RAM first crossed its threshold; None means
        # "not currently above threshold".
        self._cpu_high_since_ns: Optional[int] = None
        self._ram_high_since_ns: Optional[int] = None

    async def _get_stimulus(
        self, stim_bus: StimulusBus
    ) -> Optional[PressureSample]:
        try:
            return await asyncio.wait_for(stim_bus.pressure.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    async def _process(
        self, stim: PressureSample, policy: BioPolicy
    ) -> Optional[InterruptEvent]:
        wp = policy.worm
        dwell_ns = int(wp.dwell_ms) * 1_000_000
        t = int(stim.t_ns)

        # Track CPU dwell
        if stim.cpu > wp.cpu_pain_threshold:
            if self._cpu_high_since_ns is None:
                self._cpu_high_since_ns = t
            cpu_sustained = (t - self._cpu_high_since_ns) >= dwell_ns
        else:
            self._cpu_high_since_ns = None
            cpu_sustained = False

        # Track RAM dwell
        if stim.ram > wp.ram_pain_threshold:
            if self._ram_high_since_ns is None:
                self._ram_high_since_ns = t
            ram_sustained = (t - self._ram_high_since_ns) >= dwell_ns
        else:
            self._ram_high_since_ns = None
            ram_sustained = False

        # Sharp poke — immediate
        if stim.derivative > wp.poke_derivative:
            return InterruptEvent(
                module="worm",
                kind="ava_recoil",
                payload={
                    "cpu": float(stim.cpu),
                    "ram": float(stim.ram),
                    "pressure": float(stim.pressure),
                    "path": "poke",
                    "derivative": float(stim.derivative),
                },
            )

        if cpu_sustained or ram_sustained:
            # Reset dwell trackers so we don't instantly re-fire after
            # refractory: the event has been consumed.
            self._cpu_high_since_ns = None
            self._ram_high_since_ns = None
            return InterruptEvent(
                module="worm",
                kind="ava_recoil",
                payload={
                    "cpu": float(stim.cpu),
                    "ram": float(stim.ram),
                    "pressure": float(stim.pressure),
                    "path": "sustained",
                },
            )

        return None


async def run(
    stim_bus: StimulusBus,
    interrupt_bus: InterruptBus,
    policy_store: PolicyStore,
    snapshot: Snapshot,
    stop_event: asyncio.Event,
) -> None:
    await WormConnectome().run(
        stim_bus, interrupt_bus, policy_store, snapshot, stop_event
    )
