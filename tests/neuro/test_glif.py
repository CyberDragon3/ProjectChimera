"""Tests for the pure-numpy GLIF neuron implementation."""

from __future__ import annotations

import numpy as np

from chimera.neuro.glif import LIFNeuron, LIFPopulation

# --- single-unit --------------------------------------------------------


def _default_lif(**overrides: float) -> LIFNeuron:
    params: dict[str, float] = dict(
        tau_m_ms=20.0,
        v_rest_mv=-65.0,
        v_reset_mv=-70.0,
        v_thresh_mv=-50.0,
        refractory_ms=2.0,
        dt_ms=1.0,
        noise_sigma_mv=0.0,
    )
    params.update(overrides)
    return LIFNeuron(**params)  # type: ignore[arg-type]


def test_lif_silent_at_rest() -> None:
    neuron = _default_lif()
    spikes = [neuron.step(0.0) for _ in range(1000)]
    assert not any(spikes)
    # Membrane should sit near v_rest (noise-free).
    assert abs(neuron.v - neuron.v_rest_mv) < 1e-6


def test_lif_fires_on_strong_input() -> None:
    neuron = _default_lif()
    # Asymptote = v_rest + I. Drive I=50 mV -> asymptote -15 mV >> v_thresh.
    spike_count = sum(neuron.step(50.0) for _ in range(1000))
    assert spike_count >= 10


def test_lif_refractory_enforced() -> None:
    neuron = _default_lif(refractory_ms=10.0, dt_ms=1.0)
    spike_times: list[int] = []
    for t in range(1000):
        if neuron.step(80.0):
            spike_times.append(t)
    assert len(spike_times) >= 2
    gaps = np.diff(np.asarray(spike_times))
    # No two consecutive spikes within 10 ticks (refractory=10ms, dt=1ms).
    assert int(gaps.min()) > 10


def test_lif_deterministic_with_seeded_rng() -> None:
    rng_a = np.random.default_rng(42)
    rng_b = np.random.default_rng(42)
    a = _default_lif(noise_sigma_mv=1.0)
    b = _default_lif(noise_sigma_mv=1.0)
    a._rng = rng_a  # type: ignore[attr-defined]
    b._rng = rng_b  # type: ignore[attr-defined]

    drive = 14.0  # near-threshold (asymptote -51), noise matters
    train_a = [a.step(drive) for _ in range(500)]
    train_b = [b.step(drive) for _ in range(500)]
    assert train_a == train_b


# --- population ---------------------------------------------------------


def _default_pop(n: int = 100, **overrides: object) -> LIFPopulation:
    params: dict[str, object] = dict(
        n=n,
        excitatory_frac=0.8,
        connectivity_p=0.1,
        tau_m_ms=20.0,
        v_rest_mv=-65.0,
        v_reset_mv=-70.0,
        v_thresh_mv=-50.0,
        refractory_ms=2.0,
        dt_ms=1.0,
        noise_sigma_mv=0.0,
        rng=np.random.default_rng(0),
    )
    params.update(overrides)
    return LIFPopulation(**params)  # type: ignore[arg-type]


def test_population_shapes() -> None:
    pop = _default_pop()
    assert pop.n_exc == 80
    assert pop.n_inh == 20
    assert pop.W.shape == (100, 100)
    assert pop.mask.shape == (100, 100)
    assert not np.any(np.diag(pop.mask))


def test_population_rate_scales_with_input() -> None:
    pop = _default_pop(connectivity_p=0.1, w_exc=0.5, w_inh=-1.0)
    external = np.zeros(pop.n, dtype=np.float64)
    # Drive E cells above threshold (asymptote -35 mV); I cells see zero.
    external[: pop.n_exc] = 30.0
    for _ in range(1000):
        pop.step(external)
    assert pop.rolling_e_rate_hz > pop.rolling_i_rate_hz


def test_population_gain_raises_e_rate() -> None:
    external = np.full(100, 0.0, dtype=np.float64)
    # Subthreshold drive (asymptote -51 mV) without gain; gain=2.0 pushes over.
    external[:80] = 14.0

    pop_low = _default_pop()
    for _ in range(1000):
        pop_low.step(external, gain=1.0)
    rate_low = pop_low.rolling_e_rate_hz

    pop_high = _default_pop()
    for _ in range(1000):
        pop_high.step(external, gain=2.0)
    rate_high = pop_high.rolling_e_rate_hz

    assert rate_high > rate_low


def test_population_reset() -> None:
    pop = _default_pop()
    external = np.full(pop.n, 20.0, dtype=np.float64)
    for _ in range(1000):
        pop.step(external)
    pop.reset()
    assert np.allclose(pop._v, pop.v_rest_mv)  # type: ignore[attr-defined]
    assert pop.rolling_e_rate_hz == 0.0
    assert pop.rolling_i_rate_hz == 0.0
    assert pop.e_rate_hz == 0.0
    assert pop.i_rate_hz == 0.0
