"""Connectome base class.

Shared run-loop for the three tier-3 mini-simulators (fly / worm / mouse).
Each subclass:
  * implements `_get_stimulus(stim_bus)` to pull from the right StimulusBus queue
  * implements `_process(stim, policy)` returning an `InterruptEvent` on fire,
    or `None` if the tick should not fire.

The base `run()` loop handles:
  * stop-event early-exit (polled + woken via short queue timeouts)
  * refractory bookkeeping (skip firing while we're still "cooling down")
  * appending the fire timestamp onto the correct `Snapshot` spike deque
  * publishing the event on the `InterruptBus`
  * attaching `t_stimulus_ns` / `t_fire_ns`
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..contracts import BioPolicy, InterruptEvent
from ..event_bus import InterruptBus, PolicyStore, Snapshot, StimulusBus, now_ns
from .neural import SpikingBrain


class Connectome(ABC):
    """Abstract base for a mini-spiking connectome."""

    module: str = "base"
    refractory_s: float = 0.0
    # Subclasses that own a learned SpikingBrain expose it here so the base
    # loop can handle load/periodic-save/shutdown-save lifecycle for free.
    brain: SpikingBrain | None = None
    save_interval_s: float = 30.0

    # ---- subclass hooks --------------------------------------------------

    @abstractmethod
    async def _get_stimulus(self, stim_bus: StimulusBus) -> Optional[Any]:
        """Pull the next stimulus from the correct queue. Should return None
        on timeout so the run loop can poll the stop event."""

    @abstractmethod
    async def _process(
        self, stim: Any, policy: BioPolicy
    ) -> Optional[InterruptEvent]:
        """Return an InterruptEvent if we fire on this stimulus, else None.
        Implementations should NOT set t_fire_ns or t_stimulus_ns — the base
        loop fills those in."""

    # ---- helpers ---------------------------------------------------------

    def _stim_t_ns(self, stim: Any) -> int:
        return int(getattr(stim, "t_ns", 0) or 0)

    def _append_spike(self, snapshot: Snapshot, t_ns: int) -> None:
        deque_ = getattr(snapshot, f"{self.module}_spikes", None)
        if deque_ is not None:
            deque_.append(t_ns)

    # ---- main loop -------------------------------------------------------

    async def run(
        self,
        stim_bus: StimulusBus,
        interrupt_bus: InterruptBus,
        policy_store: PolicyStore,
        snapshot: Snapshot,
        stop_event: asyncio.Event,
    ) -> None:
        last_fire_ns: int = 0
        refractory_ns = int(self.refractory_s * 1_000_000_000)

        # Load persisted weights once at startup so learning survives restarts.
        if self.brain is not None:
            try:
                self.brain.load_if_exists()
            except Exception:
                pass
        last_save_ns = now_ns()
        save_interval_ns = int(self.save_interval_s * 1_000_000_000)

        while not stop_event.is_set():
            try:
                stim = await self._get_stimulus(stim_bus)
            except asyncio.CancelledError:
                break
            except Exception:
                # Never let a transient queue hiccup kill the connectome.
                await asyncio.sleep(0)
                continue

            if stop_event.is_set():
                break
            if stim is None:
                continue

            policy = policy_store.get()

            t_now = now_ns()
            if refractory_ns and (t_now - last_fire_ns) < refractory_ns:
                # Still cooling down — allow the subclass to update internal
                # state (so we don't miss deriv / predictor continuity) but
                # drop any would-be fire.
                try:
                    await self._process(stim, policy)
                except Exception:
                    pass
                continue

            try:
                ev = await self._process(stim, policy)
            except Exception:
                ev = None

            if ev is None:
                continue

            t_fire = now_ns()
            ev.t_stimulus_ns = self._stim_t_ns(stim)
            ev.t_fire_ns = t_fire
            last_fire_ns = t_fire

            self._append_spike(snapshot, t_fire)
            snapshot.recent_interrupts.append(ev)
            await interrupt_bus.publish(ev)

            # Periodic autosave of learned weights. Cheap — small arrays.
            if self.brain is not None and (t_fire - last_save_ns) > save_interval_ns:
                try:
                    self.brain.save()
                except Exception:
                    pass
                last_save_ns = t_fire

        # Clean shutdown — best-effort persist.
        if self.brain is not None:
            try:
                self.brain.save()
            except Exception:
                pass
