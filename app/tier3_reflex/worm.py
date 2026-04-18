"""C. elegans AVA recoil — spiking neural implementation.

The worm's input is a short-horizon history of CPU/RAM pressure and its
derivative. We feed that history as a rate vector to a tiny SNN. Pain is no
longer a fixed threshold — it's something the network learns to recognise
from the user's own load signatures (a heavy build step that the user wants
is NOT pain; an unexpected fork-bomb IS pain).

Learning signal:
  * Fire, then within ~2 s both CPU and RAM subside → +reward (pain really
    did go away — the reflex was useful).
  * Fire, then pressure keeps climbing → −reward (user's workload is
    legitimately heavy; the brain should learn to tolerate this shape).

The classic threshold/poke logic remains as a safety-net so a newly-booted
naïve brain still protects the machine on day 1.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Deque, Optional

import numpy as np

from ..contracts import BioPolicy, InterruptEvent, PressureSample
from ..event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus
from .base import Connectome
from .neural import BrainConfig, SpikingBrain


HISTORY_STEPS = 12          # ~0.5 s @ ~25 Hz pressure sampling
N_FEATURES_PER_STEP = 4     # cpu, ram, pressure, |derivative|
FEEDBACK_WINDOW_S = 2.0


class WormConnectome(Connectome):
    module = "worm"
    refractory_s = 0.500

    def __init__(self) -> None:
        self._cpu_high_since_ns: Optional[int] = None
        self._ram_high_since_ns: Optional[int] = None
        self._history: Deque[tuple[float, float, float, float]] = deque(
            maxlen=HISTORY_STEPS
        )
        self._last_stim_ns: int = 0
        self.brain = SpikingBrain(
            name="worm",
            cfg=BrainConfig(
                n_in=HISTORY_STEPS * N_FEATURES_PER_STEP,
                n_hidden=32,
                target_hidden_rate_hz=4.0,
                target_readout_rate_hz=0.3,
            ),
        )
        # Queue of (fire_t_ns, cpu_at_fire, ram_at_fire).
        self._pending_fb: Deque[tuple[int, float, float]] = deque(maxlen=16)

    async def _get_stimulus(self, stim_bus: StimulusBus) -> Optional[PressureSample]:
        try:
            return await asyncio.wait_for(stim_bus.pressure.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    def _encode(self) -> np.ndarray:
        if not self._history:
            return np.zeros(self.brain.cfg.n_in, dtype=np.float32)
        rows = list(self._history)
        # Left-pad with the oldest sample so the vector is always fixed-size.
        while len(rows) < HISTORY_STEPS:
            rows.insert(0, rows[0])
        arr = np.asarray(rows, dtype=np.float32).ravel()
        # CPU/RAM/pressure are already 0..1; derivative magnitude can be big.
        # Compress with tanh so the rate stays in [0,1].
        feat = arr.copy()
        feat[3::N_FEATURES_PER_STEP] = np.tanh(
            np.abs(arr[3::N_FEATURES_PER_STEP])
        )
        return np.clip(feat, 0.0, 1.0)

    async def _process(self, stim: PressureSample, policy: BioPolicy) -> Optional[InterruptEvent]:
        wp = policy.worm
        t = int(stim.t_ns)
        dt = ((t - self._last_stim_ns) / 1e9) if self._last_stim_ns else 0.04
        self._last_stim_ns = t
        self._history.append((
            float(stim.cpu), float(stim.ram),
            float(stim.pressure), float(stim.derivative),
        ))

        # Inhibitory gating: a low CPU-pain threshold means "the user said
        # stay alert" — sensitise the SNN. A high threshold means "the user
        # is running a heavy build, chill" — numb it.
        gate = max(0.3, min(2.5, float(wp.cpu_pain_threshold) / 0.85))
        feat = self._encode()
        fired, v_o = self.brain.step(feat, dt, gate=gate)
        self._drain_feedback(t, float(stim.cpu), float(stim.ram))

        # --- Classic dwell / poke heuristic (safety net) ------------------
        dwell_ns = int(wp.dwell_ms) * 1_000_000
        if stim.cpu > wp.cpu_pain_threshold:
            if self._cpu_high_since_ns is None:
                self._cpu_high_since_ns = t
            cpu_sustained = (t - self._cpu_high_since_ns) >= dwell_ns
        else:
            self._cpu_high_since_ns = None
            cpu_sustained = False

        if stim.ram > wp.ram_pain_threshold:
            if self._ram_high_since_ns is None:
                self._ram_high_since_ns = t
            ram_sustained = (t - self._ram_high_since_ns) >= dwell_ns
        else:
            self._ram_high_since_ns = None
            ram_sustained = False

        path: Optional[str] = None
        if stim.derivative > wp.poke_derivative:
            path = "poke"
        elif cpu_sustained or ram_sustained:
            path = "sustained"
            self._cpu_high_since_ns = None
            self._ram_high_since_ns = None
        if fired and path is None:
            path = "learned"

        if path is None:
            return None

        self._pending_fb.append((t, float(stim.cpu), float(stim.ram)))
        return InterruptEvent(
            module="worm", kind="ava_recoil",
            payload={
                "cpu": float(stim.cpu),
                "ram": float(stim.ram),
                "pressure": float(stim.pressure),
                "derivative": float(stim.derivative),
                "path": path,
                "brain_fired": bool(fired),
                "readout_v": round(v_o, 3),
            },
        )

    def _drain_feedback(self, t_ns: int, cpu_now: float, ram_now: float) -> None:
        window_ns = int(FEEDBACK_WINDOW_S * 1e9)
        while self._pending_fb and (t_ns - self._pending_fb[0][0]) >= window_ns:
            _, cpu_at_fire, ram_at_fire = self._pending_fb.popleft()
            pain_before = max(cpu_at_fire, ram_at_fire)
            pain_now = max(cpu_now, ram_now)
            if pain_before < 0.1:
                continue
            delta = pain_before - pain_now
            if delta > 0.15:
                self.brain.deliver_reward(+1.0)
            elif pain_now > pain_before + 0.05:
                # Load actually kept climbing — the fire didn't help, user
                # probably wants this workload to run.
                self.brain.deliver_reward(-0.5)


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
