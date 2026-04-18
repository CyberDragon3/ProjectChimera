"""Tier-3 neuroplastic brain tests.

These probe the learning substrate itself, independent of the fly/worm/mouse
encoders: the LIF + STDP + R-STDP + persistence guarantees we expect.
"""
from __future__ import annotations

import numpy as np

from app.tier3_reflex.neural import BrainConfig, SpikingBrain, brain_dir


def _drive(brain: SpikingBrain, x: np.ndarray, steps: int, dt: float = 0.02) -> int:
    fires = 0
    for _ in range(steps):
        fired, _ = brain.step(x, dt)
        if fired:
            fires += 1
    return fires


def test_brain_starts_quiet_on_mild_input():
    """Freshly-init brain must not spam fires on moderate input — the
    heuristic should carry the reflex until learning shapes the net."""
    brain = SpikingBrain("test_quiet", BrainConfig(n_in=32, n_hidden=16))
    x = np.full(32, 0.2, dtype=np.float32)
    fires = _drive(brain, x, steps=50)
    # 50 steps at 20 ms = 1 s. readout target is 0.5 Hz → ~0-1 fires.
    assert fires <= 3, f"fresh brain over-fires on mild input: {fires}"


def test_reward_reinforces_pattern():
    """Drive with the same pattern, reward every fire, and check the
    readout-visible weights grow (Hebbian/R-STDP working together)."""
    brain = SpikingBrain("test_reward", BrainConfig(n_in=16, n_hidden=16, eta_reward=0.05))
    # Boost baseline weights so the deliberately-quiet initial weights
    # still produce fires we can reinforce inside the test window.
    brain.st.W_ih *= 15.0
    brain.st.W_ho *= 15.0
    # Pattern the brain will see: "half the inputs on, half off".
    x = np.zeros(16, dtype=np.float32); x[:8] = 0.9

    w_ho_before = brain.st.W_ho.mean()
    fires = 0
    for _ in range(400):
        fired, _ = brain.step(x, 0.01)
        if fired:
            fires += 1
            brain.deliver_reward(+1.0)
    w_ho_after = brain.st.W_ho.mean()
    assert fires > 0, "test fixture too quiet — no fires to reward"
    assert w_ho_after > w_ho_before, (
        f"readout weights should grow with reward: {w_ho_before} -> {w_ho_after}"
    )


def test_punishment_suppresses_output():
    """After repeated −reward the brain should fire less on the same input."""
    brain = SpikingBrain("test_punish", BrainConfig(
        n_in=16, n_hidden=16,
        readout_thresh_init=0.4, eta_reward=0.05,
    ))
    # Crank initial weights so we actually get fires to punish.
    brain.st.W_ih *= 10.0
    brain.st.W_ho *= 10.0

    x = np.full(16, 0.6, dtype=np.float32)
    # Phase A: count fires naturally.
    early = _drive(brain, x, steps=150, dt=0.01)
    # Phase B: punish every fire we now produce.
    punished = 0
    for _ in range(300):
        fired, _ = brain.step(x, 0.01)
        if fired:
            brain.deliver_reward(-1.0)
            punished += 1
    # Phase C: count fires again without feedback.
    late = _drive(brain, x, steps=150, dt=0.01)
    assert late <= early, (
        f"punishment didn't suppress firing: early={early} punished={punished} late={late}"
    )


def test_save_and_load_round_trip(tmp_path, monkeypatch):
    """Learned weights must persist across process restarts."""
    monkeypatch.setenv("CHIMERA_BRAIN_DIR", str(tmp_path))
    cfg = BrainConfig(n_in=8, n_hidden=8)
    a = SpikingBrain("round_trip", cfg)
    # Tamper with weights so we know save/load moved real data.
    a.st.W_ih += 0.5
    a.st.W_ho += 0.5
    a.save()

    b = SpikingBrain("round_trip", cfg)
    assert b.load_if_exists() is True
    assert np.allclose(b.st.W_ih, a.st.W_ih)
    assert np.allclose(b.st.W_ho, a.st.W_ho)
    # And a json metadata file landed next to the npz.
    assert (brain_dir() / "round_trip.json").exists()


def test_shape_mismatch_rejected_safely(tmp_path, monkeypatch):
    """If a user changes n_in/n_hidden between runs, load should decline
    (return False) rather than crash or silently corrupt."""
    monkeypatch.setenv("CHIMERA_BRAIN_DIR", str(tmp_path))
    SpikingBrain("shape_mismatch", BrainConfig(n_in=8, n_hidden=8)).save()
    b = SpikingBrain("shape_mismatch", BrainConfig(n_in=16, n_hidden=8))
    assert b.load_if_exists() is False


def test_homeostasis_clamps_threshold():
    """Overdriven brain must not let readout_thresh run away to infinity."""
    brain = SpikingBrain("test_homeo", BrainConfig(n_in=8, n_hidden=8))
    brain.st.W_ih *= 50.0
    brain.st.W_ho *= 50.0
    x = np.ones(8, dtype=np.float32)
    for _ in range(500):
        brain.step(x, 0.01)
    assert 0.2 <= brain.st.readout_thresh <= 5.0
