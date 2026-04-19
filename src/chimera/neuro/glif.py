"""Generalized Leaky Integrate-and-Fire (GLIF) neurons.

Pure-numpy implementation. No asyncio, no Bus. Vectorized Euler integration at
fixed dt. Units: millivolts (mV) and milliseconds (ms). The input current is
expressed in mV (an effective drive that asymptotes the membrane), which is a
common engineering simplification for LIF models.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class LIFNeuron:
    """Single-unit leaky integrate-and-fire neuron."""

    def __init__(
        self,
        *,
        tau_m_ms: float,
        v_rest_mv: float,
        v_reset_mv: float,
        v_thresh_mv: float,
        refractory_ms: float,
        dt_ms: float,
        noise_sigma_mv: float = 0.0,
        rng: np.random.Generator | None = None,
    ) -> None:
        self.tau_m_ms = float(tau_m_ms)
        self.v_rest_mv = float(v_rest_mv)
        self.v_reset_mv = float(v_reset_mv)
        self.v_thresh_mv = float(v_thresh_mv)
        self.refractory_ms = float(refractory_ms)
        self.dt_ms = float(dt_ms)
        self.noise_sigma_mv = float(noise_sigma_mv)
        self._rng: np.random.Generator = (
            rng if rng is not None else np.random.default_rng()
        )

        self._refractory_ticks: int = max(
            0, round(self.refractory_ms / self.dt_ms)
        )
        self._v: float = self.v_rest_mv
        self._refr_counter: int = 0

    @property
    def v(self) -> float:
        return self._v

    def reset(self) -> None:
        self._v = self.v_rest_mv
        self._refr_counter = 0

    def step(self, current_mv: float) -> bool:
        """Advance one tick. Returns True if the neuron spiked this tick."""
        if self._refr_counter > 0:
            self._refr_counter -= 1
            self._v = self.v_reset_mv
            return False

        noise = 0.0
        if self.noise_sigma_mv > 0.0:
            noise = float(self._rng.normal(0.0, self.noise_sigma_mv))

        dv = (
            -(self._v - self.v_rest_mv) / self.tau_m_ms
            + float(current_mv) / self.tau_m_ms
        )
        self._v = self._v + dv * self.dt_ms + noise

        if self._v >= self.v_thresh_mv:
            self._v = self.v_reset_mv
            self._refr_counter = self._refractory_ticks
            return True
        return False


class LIFPopulation:
    """Sparse recurrent excitatory/inhibitory LIF population.

    Connectivity is stored as a dense ``(n, n)`` boolean mask (cheap at n=100
    — 10 kB — and faster than scipy.sparse at this size). The weight matrix
    ``W = mask * sign``: excitatory (pre-synaptic) rows use ``+w_exc``,
    inhibitory rows use ``-|w_inh|``. The diagonal is forced to False (no
    self-loops). ``gain`` multiplies the external drive to excitatory cells
    only — this models dopaminergic modulation of the E population.
    """

    _ROLLING_MAXLEN: int = 100

    def __init__(
        self,
        *,
        n: int,
        excitatory_frac: float,
        connectivity_p: float,
        tau_m_ms: float,
        v_rest_mv: float,
        v_reset_mv: float,
        v_thresh_mv: float,
        refractory_ms: float,
        dt_ms: float,
        noise_sigma_mv: float = 0.0,
        w_exc: float = 1.0,
        w_inh: float = -2.0,
        rng: np.random.Generator | None = None,
    ) -> None:
        if n <= 0:
            raise ValueError("n must be positive")
        if not 0.0 <= excitatory_frac <= 1.0:
            raise ValueError("excitatory_frac must be in [0, 1]")
        if not 0.0 <= connectivity_p <= 1.0:
            raise ValueError("connectivity_p must be in [0, 1]")

        self.n = int(n)
        self.excitatory_frac = float(excitatory_frac)
        self.connectivity_p = float(connectivity_p)
        self.tau_m_ms = float(tau_m_ms)
        self.v_rest_mv = float(v_rest_mv)
        self.v_reset_mv = float(v_reset_mv)
        self.v_thresh_mv = float(v_thresh_mv)
        self.refractory_ms = float(refractory_ms)
        self.dt_ms = float(dt_ms)
        self.noise_sigma_mv = float(noise_sigma_mv)
        self.w_exc = float(w_exc)
        self.w_inh = float(w_inh)
        self._rng: np.random.Generator = (
            rng if rng is not None else np.random.default_rng()
        )

        n_exc = round(self.n * self.excitatory_frac)
        self.n_exc: int = n_exc
        self.n_inh: int = self.n - n_exc

        # is_exc[i] = True iff unit i is excitatory. First n_exc units are E.
        self.is_exc: np.ndarray = np.zeros(self.n, dtype=bool)
        self.is_exc[:n_exc] = True

        # Connectivity mask: mask[i, j] = True iff j projects to i.
        mask = self._rng.random((self.n, self.n)) < self.connectivity_p
        np.fill_diagonal(mask, False)
        self.mask: np.ndarray = mask

        # Weight matrix: column j signed by pre-synaptic type.
        signs = np.where(self.is_exc, self.w_exc, self.w_inh).astype(np.float64)
        self.W: np.ndarray = self.mask.astype(np.float64) * signs[np.newaxis, :]

        self._refractory_ticks: int = max(
            0, round(self.refractory_ms / self.dt_ms)
        )

        self._v: np.ndarray = np.full(self.n, self.v_rest_mv, dtype=np.float64)
        self._refr_counter: np.ndarray = np.zeros(self.n, dtype=np.int64)
        self._last_spikes: np.ndarray = np.zeros(self.n, dtype=bool)

        self._e_spike_history: deque[int] = deque(maxlen=self._ROLLING_MAXLEN)
        self._i_spike_history: deque[int] = deque(maxlen=self._ROLLING_MAXLEN)

    # --- public API ---------------------------------------------------

    def reset(self) -> None:
        self._v.fill(self.v_rest_mv)
        self._refr_counter.fill(0)
        self._last_spikes.fill(False)
        self._e_spike_history.clear()
        self._i_spike_history.clear()

    def step(self, external_current: np.ndarray, gain: float = 1.0) -> np.ndarray:
        """Advance one tick and return a bool spike vector of shape (n,)."""
        external = np.asarray(external_current, dtype=np.float64)
        if external.shape != (self.n,):
            raise ValueError(
                f"external_current shape {external.shape} != ({self.n},)"
            )

        # Dopamine-like gain multiplies the E-cell external drive only.
        drive = external.copy()
        if gain != 1.0:
            drive[self.is_exc] *= float(gain)

        # Recurrent input from last tick's spikes.
        recurrent = self.W @ self._last_spikes.astype(np.float64)
        i_total = drive + recurrent

        # Noise.
        if self.noise_sigma_mv > 0.0:
            noise = self._rng.normal(0.0, self.noise_sigma_mv, size=self.n)
        else:
            noise = np.zeros(self.n, dtype=np.float64)

        refr_mask = self._refr_counter > 0

        # Euler step for non-refractory units.
        dv = (
            -(self._v - self.v_rest_mv) / self.tau_m_ms
            + i_total / self.tau_m_ms
        )
        self._v = self._v + dv * self.dt_ms + noise

        # Refractory units held at v_reset and their counters decremented.
        self._v[refr_mask] = self.v_reset_mv
        self._refr_counter[refr_mask] -= 1

        # Threshold crossing (only for non-refractory units).
        spiked = (self._v >= self.v_thresh_mv) & (~refr_mask)
        self._v[spiked] = self.v_reset_mv
        self._refr_counter[spiked] = self._refractory_ticks

        self._last_spikes = spiked

        e_count = int(np.count_nonzero(spiked[: self.n_exc]))
        i_count = int(np.count_nonzero(spiked[self.n_exc :]))
        self._e_spike_history.append(e_count)
        self._i_spike_history.append(i_count)

        return spiked

    # --- rate accessors ----------------------------------------------

    @property
    def e_rate_hz(self) -> float:
        """Instantaneous E-population rate from the last tick (Hz)."""
        if not self._e_spike_history or self.n_exc == 0:
            return 0.0
        last = self._e_spike_history[-1]
        return last / self.n_exc / (self.dt_ms / 1000.0)

    @property
    def i_rate_hz(self) -> float:
        """Instantaneous I-population rate from the last tick (Hz)."""
        if not self._i_spike_history or self.n_inh == 0:
            return 0.0
        last = self._i_spike_history[-1]
        return last / self.n_inh / (self.dt_ms / 1000.0)

    @property
    def rolling_e_rate_hz(self) -> float:
        """Mean E rate over the last 100 ticks (Hz)."""
        if not self._e_spike_history or self.n_exc == 0:
            return 0.0
        total = sum(self._e_spike_history)
        window_s = len(self._e_spike_history) * (self.dt_ms / 1000.0)
        if window_s == 0.0:
            return 0.0
        return total / self.n_exc / window_s

    @property
    def rolling_i_rate_hz(self) -> float:
        """Mean I rate over the last 100 ticks (Hz)."""
        if not self._i_spike_history or self.n_inh == 0:
            return 0.0
        total = sum(self._i_spike_history)
        window_s = len(self._i_spike_history) * (self.dt_ms / 1000.0)
        if window_s == 0.0:
            return 0.0
        return total / self.n_inh / window_s
