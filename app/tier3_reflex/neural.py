"""Spiking neural substrate with Hebbian (STDP) + reward-modulated plasticity.

Each animal module owns a `SpikingBrain`:
  input layer (rate code)  →  hidden LIF population  →  readout LIF

Biology modelled:
  * Leaky integrate-and-fire neurons with refractory period
  * Pre/post synaptic traces for pair-wise STDP
  * Eligibility trace gated by a dopamine-like reward signal (R-STDP)
  * Synaptic-scaling homeostasis to keep hidden firing rate near target
  * Adaptive readout threshold (intrinsic plasticity) so the network stays
    responsive even as feature statistics drift
  * Weights persist to disk per-module, so learning survives restarts

No torch dependency — plain numpy, O(n_in * n_hidden) per step, cheap enough
to run inside each reflex loop on CPU.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Disk layout for learned weights
# ---------------------------------------------------------------------------

def brain_dir() -> Path:
    """Where per-module weight files live. Same family of paths as
    setup_check.user_config_dir so brains live alongside the user config.

    Set ``CHIMERA_BRAIN_DIR`` to redirect (used by tests and for letting
    power users keep multiple "brain profiles" side-by-side)."""
    override = os.environ.get("CHIMERA_BRAIN_DIR")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Chimera" / "brains"
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".chimera" / "brains"


# ---------------------------------------------------------------------------
# Core spiking brain
# ---------------------------------------------------------------------------

@dataclass
class BrainConfig:
    n_in: int
    n_hidden: int = 32
    # Membrane dynamics (per-second time constants; actual leak computed from dt).
    tau_mem_s: float = 0.050
    refractory_s: float = 0.010
    v_thresh: float = 1.0
    v_reset: float = 0.0
    # Plasticity
    tau_pre_s: float = 0.020
    tau_post_s: float = 0.020
    tau_eligibility_s: float = 1.5
    eta_stdp: float = 0.005
    eta_reward: float = 0.02
    # Homeostasis
    target_hidden_rate_hz: float = 5.0
    target_readout_rate_hz: float = 0.5
    eta_homeo: float = 0.002
    # Bounds
    w_min: float = 0.0
    w_max: float = 4.0
    # Initial readout threshold (intrinsic plasticity drifts this)
    readout_thresh_init: float = 1.0


@dataclass
class BrainState:
    W_ih: np.ndarray                 # (n_hidden, n_in)
    W_ho: np.ndarray                 # (n_hidden,)
    v_h: np.ndarray                  # hidden membrane
    v_o: float                       # readout membrane
    refr_h: np.ndarray               # refractory countdown seconds
    refr_o: float
    pre_trace_in: np.ndarray         # (n_in,)
    post_trace_h: np.ndarray         # (n_hidden,)
    pre_trace_h: np.ndarray          # (n_hidden,) for ho synapse
    post_trace_o: float
    elig_ih: np.ndarray              # (n_hidden, n_in)
    elig_ho: np.ndarray              # (n_hidden,)
    readout_thresh: float
    hidden_rate_ema: np.ndarray      # (n_hidden,) running spike rate
    readout_rate_ema: float
    # lifetime counters — useful telemetry
    total_steps: int = 0
    total_hidden_spikes: int = 0
    total_readout_spikes: int = 0
    total_reward: float = 0.0


class SpikingBrain:
    """Small LIF network with STDP + reward-modulated plasticity.

    Typical usage from a connectome:
        brain = SpikingBrain(name="fly", cfg=BrainConfig(n_in=64))
        brain.load_if_exists()
        fired, readout_v = brain.step(x, dt)
        ...
        brain.deliver_reward(+1.0)   # when fire turns out correct
        brain.deliver_reward(-1.0)   # when it was a false positive
        brain.save()                 # periodically, or on shutdown
    """

    SCHEMA_VERSION = 1

    def __init__(self, name: str, cfg: BrainConfig, seed: int = 0xC0FFEE) -> None:
        self.name = name
        self.cfg = cfg
        self._rng = np.random.default_rng(seed)

        n_in, n_h = cfg.n_in, cfg.n_hidden
        # Small positive init, scaled by 1/sqrt(fan-in) so expected per-step
        # drive stays well below v_thresh — the network is deliberately
        # quiet on day 1. The heuristic in each connectome carries the
        # reflex until STDP + reward has shaped weights to match the user.
        scale_ih = 0.1 / max(1.0, math.sqrt(n_in))
        scale_ho = 0.15 / max(1.0, math.sqrt(n_h))
        W_ih = np.abs(self._rng.normal(0.0, scale_ih, size=(n_h, n_in))).astype(np.float32)
        W_ho = np.abs(self._rng.normal(0.0, scale_ho, size=(n_h,))).astype(np.float32)

        self.st = BrainState(
            W_ih=W_ih,
            W_ho=W_ho,
            v_h=np.zeros(n_h, dtype=np.float32),
            v_o=0.0,
            refr_h=np.zeros(n_h, dtype=np.float32),
            refr_o=0.0,
            pre_trace_in=np.zeros(n_in, dtype=np.float32),
            post_trace_h=np.zeros(n_h, dtype=np.float32),
            pre_trace_h=np.zeros(n_h, dtype=np.float32),
            post_trace_o=0.0,
            elig_ih=np.zeros((n_h, n_in), dtype=np.float32),
            elig_ho=np.zeros(n_h, dtype=np.float32),
            readout_thresh=float(cfg.readout_thresh_init),
            hidden_rate_ema=np.zeros(n_h, dtype=np.float32),
            readout_rate_ema=0.0,
        )

    # ------------------------------------------------------------------
    # Main step — drive the net with an input rate vector, return readout
    # ------------------------------------------------------------------

    def step(self, x: np.ndarray, dt: float, gate: float = 1.0) -> tuple[bool, float]:
        """Advance one tick.

        Args:
            x: non-negative input "rate" vector, shape (n_in,). Values near 0
               rarely spike; values near 1 spike most ticks. Caller is
               responsible for normalising features into this range.
            dt: seconds since the previous step for this brain.
            gate: LLM-driven inhibitory multiplier on the readout threshold.
                ``gate < 1`` → reflex sensitised (fires easier — Jarvis told
                the brain "pay attention"); ``gate > 1`` → reflex numbed
                (harder to fire — "ignore this"). Applied transiently for
                this step only; does not mutate the learned threshold.

        Returns:
            (readout_fired, readout_v) — boolean spike and the current
            readout membrane potential.
        """
        cfg = self.cfg
        st = self.st
        dt = max(float(dt), 1e-4)
        st.total_steps += 1

        # Stochastic input spikes: Bernoulli(clip(x, 0, 1)).
        x = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
        in_spikes = (self._rng.random(x.shape, dtype=np.float32) < x).astype(np.float32)

        # --- Hidden LIF update --------------------------------------------
        leak_m = math.exp(-dt / cfg.tau_mem_s)
        drive_h = st.W_ih @ in_spikes                         # (n_h,)
        active_h = st.refr_h <= 0.0
        st.v_h = np.where(active_h, st.v_h * leak_m + drive_h, st.v_h)
        hidden_spikes = (st.v_h >= cfg.v_thresh) & active_h
        st.v_h = np.where(hidden_spikes, cfg.v_reset, st.v_h)
        st.refr_h = np.where(
            hidden_spikes, cfg.refractory_s,
            np.maximum(st.refr_h - dt, 0.0),
        )
        h_fired = hidden_spikes.astype(np.float32)
        st.total_hidden_spikes += int(h_fired.sum())

        # --- Readout LIF update -------------------------------------------
        drive_o = float(st.W_ho @ h_fired)
        gate_eff = float(max(0.1, min(5.0, gate)))
        effective_thresh = st.readout_thresh * gate_eff
        readout_fired = False
        if st.refr_o <= 0.0:
            st.v_o = st.v_o * leak_m + drive_o
            if st.v_o >= effective_thresh:
                readout_fired = True
                st.v_o = cfg.v_reset
                st.refr_o = cfg.refractory_s
                st.total_readout_spikes += 1
        else:
            st.refr_o = max(st.refr_o - dt, 0.0)
        o_fired = 1.0 if readout_fired else 0.0

        # --- Synaptic traces (exponential decay) --------------------------
        pre_decay_in = math.exp(-dt / cfg.tau_pre_s)
        post_decay_h = math.exp(-dt / cfg.tau_post_s)
        pre_decay_h = math.exp(-dt / cfg.tau_pre_s)
        post_decay_o = math.exp(-dt / cfg.tau_post_s)

        st.pre_trace_in = st.pre_trace_in * pre_decay_in + in_spikes
        st.post_trace_h = st.post_trace_h * post_decay_h + h_fired
        st.pre_trace_h = st.pre_trace_h * pre_decay_h + h_fired
        st.post_trace_o = st.post_trace_o * post_decay_o + o_fired

        # --- STDP eligibility --------------------------------------------
        # ih: LTP when hidden post-spikes *after* an input pre-trace; LTD
        # when the input pre-fires after a hidden post-trace.
        if h_fired.any():
            ltp_ih = np.outer(h_fired, st.pre_trace_in)          # (n_h, n_in)
            st.elig_ih += cfg.eta_stdp * ltp_ih
        if in_spikes.any():
            ltd_ih = np.outer(st.post_trace_h, in_spikes)
            st.elig_ih -= cfg.eta_stdp * ltd_ih

        # ho: same story for hidden→readout.
        if o_fired:
            st.elig_ho += cfg.eta_stdp * st.pre_trace_h
        if h_fired.any():
            st.elig_ho -= cfg.eta_stdp * st.post_trace_o * h_fired

        # --- Eligibility decay (window during which reward still binds) --
        elig_decay = math.exp(-dt / cfg.tau_eligibility_s)
        st.elig_ih *= elig_decay
        st.elig_ho *= elig_decay

        # --- Homeostatic synaptic scaling on hidden layer ----------------
        rate_ema_decay = math.exp(-dt / 5.0)                     # 5 s window
        st.hidden_rate_ema = st.hidden_rate_ema * rate_ema_decay + h_fired * (1.0 - rate_ema_decay) / dt
        st.readout_rate_ema = st.readout_rate_ema * rate_ema_decay + o_fired * (1.0 - rate_ema_decay) / dt

        # Scale hidden-incoming weights toward target rate.
        err_h = (cfg.target_hidden_rate_hz - st.hidden_rate_ema)  # (n_h,)
        st.W_ih += cfg.eta_homeo * err_h[:, None] * st.W_ih * dt
        np.clip(st.W_ih, cfg.w_min, cfg.w_max, out=st.W_ih)

        # Intrinsic plasticity: nudge readout threshold toward target spike rate.
        st.readout_thresh += cfg.eta_homeo * (st.readout_rate_ema - cfg.target_readout_rate_hz) * dt * 20.0
        st.readout_thresh = float(np.clip(st.readout_thresh, 0.2, 5.0))

        return readout_fired, float(st.v_o)

    # ------------------------------------------------------------------
    # Reward / punishment (R-STDP)
    # ------------------------------------------------------------------

    def deliver_reward(self, r: float) -> None:
        """Apply a scalar reward (positive = good, negative = bad) to the
        currently-held eligibility traces, committing learning."""
        r = float(np.clip(r, -2.0, 2.0))
        if r == 0.0:
            return
        cfg = self.cfg
        st = self.st
        st.total_reward += r
        st.W_ih += cfg.eta_reward * r * st.elig_ih
        st.W_ho += cfg.eta_reward * r * st.elig_ho
        np.clip(st.W_ih, cfg.w_min, cfg.w_max, out=st.W_ih)
        np.clip(st.W_ho, cfg.w_min, cfg.w_max, out=st.W_ho)
        # Partial eligibility decay so a reward doesn't keep biasing future fires.
        st.elig_ih *= 0.3
        st.elig_ho *= 0.3

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _path(self) -> Path:
        d = brain_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.name}.npz"

    def _meta_path(self) -> Path:
        return self._path().with_suffix(".json")

    def save(self) -> None:
        st = self.st
        np.savez(
            self._path(),
            W_ih=st.W_ih, W_ho=st.W_ho,
            v_h=st.v_h, v_o=np.float32(st.v_o),
            refr_h=st.refr_h, refr_o=np.float32(st.refr_o),
            pre_trace_in=st.pre_trace_in,
            post_trace_h=st.post_trace_h,
            pre_trace_h=st.pre_trace_h,
            post_trace_o=np.float32(st.post_trace_o),
            elig_ih=st.elig_ih, elig_ho=st.elig_ho,
            readout_thresh=np.float32(st.readout_thresh),
            hidden_rate_ema=st.hidden_rate_ema,
            readout_rate_ema=np.float32(st.readout_rate_ema),
        )
        meta = {
            "schema": self.SCHEMA_VERSION,
            "name": self.name,
            "saved_at": time.time(),
            "n_in": self.cfg.n_in,
            "n_hidden": self.cfg.n_hidden,
            "total_steps": st.total_steps,
            "total_hidden_spikes": st.total_hidden_spikes,
            "total_readout_spikes": st.total_readout_spikes,
            "total_reward": st.total_reward,
            "readout_thresh": st.readout_thresh,
        }
        self._meta_path().write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def load_if_exists(self) -> bool:
        p = self._path()
        if not p.exists():
            return False
        try:
            data = np.load(p, allow_pickle=False)
            if data["W_ih"].shape != self.st.W_ih.shape:
                # Shape mismatch (user changed n_in / n_hidden) — discard.
                return False
            st = self.st
            st.W_ih = data["W_ih"].astype(np.float32)
            st.W_ho = data["W_ho"].astype(np.float32)
            st.v_h = data["v_h"].astype(np.float32)
            st.v_o = float(data["v_o"])
            st.refr_h = data["refr_h"].astype(np.float32)
            st.refr_o = float(data["refr_o"])
            st.pre_trace_in = data["pre_trace_in"].astype(np.float32)
            st.post_trace_h = data["post_trace_h"].astype(np.float32)
            st.pre_trace_h = data["pre_trace_h"].astype(np.float32)
            st.post_trace_o = float(data["post_trace_o"])
            st.elig_ih = data["elig_ih"].astype(np.float32)
            st.elig_ho = data["elig_ho"].astype(np.float32)
            st.readout_thresh = float(data["readout_thresh"])
            st.hidden_rate_ema = data["hidden_rate_ema"].astype(np.float32)
            st.readout_rate_ema = float(data["readout_rate_ema"])
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        st = self.st
        return {
            "name": self.name,
            "steps": st.total_steps,
            "hidden_spikes": st.total_hidden_spikes,
            "readout_spikes": st.total_readout_spikes,
            "total_reward": round(st.total_reward, 3),
            "readout_thresh": round(st.readout_thresh, 3),
            "hidden_rate_mean": float(round(st.hidden_rate_ema.mean(), 3)),
            "readout_rate": round(st.readout_rate_ema, 4),
            "w_ih_mean": float(round(st.W_ih.mean(), 3)),
            "w_ih_std": float(round(st.W_ih.std(), 3)),
            "w_ho_mean": float(round(st.W_ho.mean(), 3)),
        }
