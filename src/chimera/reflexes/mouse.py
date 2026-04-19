"""Mouse reflex — semantic context filter + cortex veto publisher.

Keeps the original ``context.active_window`` enrichment (creator-apps → intentional).
Adds ``cortex.protect_foreground`` veto when a CPU spike is sourced by the
currently foregrounded PID — the BMTKCortex instructs the Worm to stand down
so the user's active work isn't throttled (see design §5.2).

Two classes are exposed:

- :class:`MouseReflex` — legacy binary classifier (foreground spike → protect).
- :class:`MouseCortex` — 100-neuron LIF E/I population whose rolling E-rate
  gates the same veto. Dopamine multiplies the E-cell drive so recent hit
  streaks make the cortex hunt more eagerly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
import structlog

from chimera.bus import Bus, Event
from chimera.neuro.glif import LIFPopulation

if TYPE_CHECKING:
    from chimera.config import NeuroCfg

log = structlog.get_logger(__name__)

DEFAULT_CREATOR_APPS: frozenset[str] = frozenset(
    {
        "blender.exe", "premiere.exe", "adobe premiere pro.exe", "aftereffects.exe",
        "davinci resolve.exe", "unreal.exe", "unrealeditor.exe", "unity.exe",
        "ffmpeg.exe", "handbrake.exe", "code.exe", "cursor.exe",
        "pycharm64.exe", "devenv.exe", "obs64.exe", "obs32.exe",
    }
)


class MouseReflex:
    def __init__(self, bus: Bus, creator_apps: frozenset[str] = DEFAULT_CREATOR_APPS) -> None:
        self._bus = bus
        self._creators = frozenset(a.lower() for a in creator_apps)
        self._foreground_pid: int | None = None
        self._foreground_exe: str | None = None

    def _publish_protect(self, on: bool) -> None:
        self._bus.publish(
            Event(
                topic="cortex.protect_foreground",
                payload={"on": on, "foreground_pid": self._foreground_pid},
                ts=time.monotonic(),
            )
        )

    async def _handle_window(self, event: Event) -> None:
        try:
            pid_raw = event.payload.get("pid")
            self._foreground_pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            self._foreground_pid = None
        exe = str(event.payload.get("exe", "")).lower()
        self._foreground_exe = exe or None
        intentional = exe in self._creators
        self._bus.publish(
            Event(
                topic="context.active_window",
                payload={**event.payload, "intentional": intentional},
                ts=time.monotonic(),
            )
        )
        self._publish_protect(False)
        log.info(
            "reflex.mouse.classified",
            exe=exe, pid=self._foreground_pid, intentional=intentional,
        )

    async def _handle_spike(self, event: Event) -> None:
        try:
            spike_pid = int(event.payload.get("pid", -1))
        except (TypeError, ValueError):
            return
        if self._foreground_pid is not None and spike_pid == self._foreground_pid:
            self._publish_protect(True)
            log.info(
                "reflex.mouse.protect_on",
                pid=spike_pid, exe=event.payload.get("exe"),
            )

    async def run(self) -> None:
        win_q = self._bus.subscribe("window.foreground")
        spike_q = self._bus.subscribe("cpu.spike")
        log.info("reflex.mouse.start", creator_apps=len(self._creators))
        win_task: asyncio.Task[Event] = asyncio.create_task(win_q.get())
        spike_task: asyncio.Task[Event] = asyncio.create_task(spike_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {win_task, spike_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        event = t.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("reflex.mouse.recv_failed", error=str(e))
                        event = None
                    if t is win_task:
                        win_task = asyncio.create_task(win_q.get())
                        if event is not None:
                            await self._handle_window(event)
                    elif t is spike_task:
                        spike_task = asyncio.create_task(spike_q.get())
                        if event is not None:
                            await self._handle_spike(event)
        finally:
            for pending in (win_task, spike_task):
                if not pending.done():
                    pending.cancel()
            self._bus.unsubscribe("window.foreground", win_q)
            self._bus.unsubscribe("cpu.spike", spike_q)


class MouseCortex:
    """100-neuron E/I LIF population replacing the semantic classifier.

    E-cells represent "user is actively engaged with this PID"; I-cells
    inhibit. The rolling E-population rate is thresholded to publish
    ``cortex.protect_foreground`` with the legacy schema. Dopamine level
    multiplies the E-cell external drive (``gain`` arg of
    :meth:`LIFPopulation.step`).
    """

    _TONIC_I_DRIVE_MV: float = 3.0
    _BURST_WINDOW_S: float = 0.2
    _BURST_DRIVE_MV: float = 8.0

    def __init__(
        self,
        bus: Bus,
        *,
        neuro_cfg: NeuroCfg,
        creator_apps: frozenset[str] = DEFAULT_CREATOR_APPS,
        rng: np.random.Generator | None = None,
    ) -> None:
        self._bus = bus
        self._cfg = neuro_cfg
        self._creators = frozenset(a.lower() for a in creator_apps)
        self._rng = rng if rng is not None else np.random.default_rng()

        self._tick_hz: int = int(neuro_cfg.tick_hz)
        self._dt_ms: float = 1000.0 / float(self._tick_hz)
        self._tick_interval_s: float = 1.0 / float(self._tick_hz)

        self._pop = LIFPopulation(
            n=int(neuro_cfg.mouse_population),
            excitatory_frac=float(neuro_cfg.mouse_excitatory_frac),
            connectivity_p=float(neuro_cfg.connectivity_p),
            tau_m_ms=float(neuro_cfg.tau_m_ms),
            v_rest_mv=float(neuro_cfg.v_rest_mv),
            v_reset_mv=float(neuro_cfg.v_reset_mv),
            v_thresh_mv=float(neuro_cfg.v_thresh_mv),
            refractory_ms=float(neuro_cfg.refractory_ms),
            dt_ms=self._dt_ms,
            noise_sigma_mv=float(neuro_cfg.noise_sigma_mv),
            rng=self._rng,
        )

        self._foreground_pid: int | None = None
        self._foreground_exe: str | None = None
        self._creator_active: bool = False
        self._dopamine_level: float = 0.0

        self._burst_ticks_total: int = max(1, int(self._BURST_WINDOW_S * self._tick_hz))
        self._burst_ticks_remaining: int = 0

        # Transition-only publish state (None = never sent yet).
        self._protect_on: bool | None = None

        self._stop: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------ I/O

    def _publish_protect(self, on: bool) -> None:
        self._bus.publish(
            Event(
                topic="cortex.protect_foreground",
                payload={"on": on, "foreground_pid": self._foreground_pid},
                ts=time.monotonic(),
            )
        )

    def _publish_rate(self) -> None:
        self._bus.publish(
            Event(
                topic="neuro.mouse.rate",
                payload={
                    "e_rate_hz": float(self._pop.e_rate_hz),
                    "i_rate_hz": float(self._pop.i_rate_hz),
                },
                ts=time.monotonic(),
            )
        )

    def _publish_active_window(self, source_payload: dict[str, object]) -> None:
        self._bus.publish(
            Event(
                topic="context.active_window",
                payload={**source_payload, "intentional": self._creator_active},
                ts=time.monotonic(),
            )
        )

    # --------------------------------------------------------------- event handlers

    def _on_window(self, event: Event) -> None:
        try:
            pid_raw = event.payload.get("pid")
            self._foreground_pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            self._foreground_pid = None
        exe = str(event.payload.get("exe", "")).lower()
        self._foreground_exe = exe or None
        self._creator_active = exe in self._creators
        self._publish_active_window(dict(event.payload))
        # Fresh computation for the new foreground.
        self._pop.reset()
        self._burst_ticks_remaining = 0
        # Force protect-off transition on any window change — user switched apps.
        if self._protect_on is not False:
            self._protect_on = False
            self._publish_protect(False)
        log.info(
            "reflex.mouse.cortex.classified",
            exe=exe, pid=self._foreground_pid, intentional=self._creator_active,
        )

    def _on_spike(self, event: Event) -> None:
        try:
            spike_pid = int(event.payload.get("pid", -1))
        except (TypeError, ValueError):
            return
        if self._foreground_pid is not None and spike_pid == self._foreground_pid:
            self._burst_ticks_remaining = self._burst_ticks_total

    def _on_dopamine(self, event: Event) -> None:
        try:
            self._dopamine_level = float(event.payload.get("level", 0.0))
        except (TypeError, ValueError):
            self._dopamine_level = 0.0

    # ---------------------------------------------------------------- tick

    def _build_external_current(self) -> np.ndarray:
        n = self._pop.n
        external = np.zeros(n, dtype=np.float64)
        # Tonic drive to I-cells so they are not silent.
        external[~self._pop.is_exc] += self._TONIC_I_DRIVE_MV
        # E-cell drive when the foreground is a creator app.
        if self._creator_active:
            external[self._pop.is_exc] += float(self._cfg.mouse_creator_drive_mv)
        # Transient burst on foreground CPU spike.
        if self._burst_ticks_remaining > 0:
            external[self._pop.is_exc] += self._BURST_DRIVE_MV
            self._burst_ticks_remaining -= 1
        return external

    def _tick(self) -> None:
        external = self._build_external_current()
        gain = 1.0 + float(self._cfg.dopamine_gain_coeff) * self._dopamine_level
        self._pop.step(external, gain=gain)
        self._publish_rate()
        above = self._pop.rolling_e_rate_hz > float(self._cfg.mouse_rate_threshold_hz)
        if above and self._protect_on is not True:
            self._protect_on = True
            self._publish_protect(True)
        elif (not above) and self._protect_on is True:
            self._protect_on = False
            self._publish_protect(False)

    # --------------------------------------------------------------- runners

    async def _drain(
        self, q: asyncio.Queue[Event], handler: Callable[[Event], None]
    ) -> None:
        while True:
            try:
                event = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                handler(event)
            except Exception as e:  # pragma: no cover — defensive
                log.warning("reflex.mouse.cortex.handler_failed", error=str(e))

    async def run(self) -> None:
        win_q = self._bus.subscribe("window.foreground")
        spike_q = self._bus.subscribe("cpu.spike")
        dopa_q = self._bus.subscribe("neuro.dopamine")

        # One-shot timing measurement at startup to decide whether to offload.
        probe_ext = np.zeros(self._pop.n, dtype=np.float64)
        t0 = time.perf_counter_ns()
        self._pop.step(probe_ext, gain=1.0)
        step_ns = time.perf_counter_ns() - t0
        self._pop.reset()
        log.info(
            "reflex.mouse.cortex.start",
            n=self._pop.n,
            tick_hz=self._tick_hz,
            step_us=step_ns / 1000.0,
            creator_apps=len(self._creators),
        )

        try:
            next_tick = time.monotonic()
            while not self._stop.is_set():
                # Drain each subscription queue — deterministic tick pacing.
                await self._drain(win_q, self._on_window)
                await self._drain(spike_q, self._on_spike)
                await self._drain(dopa_q, self._on_dopamine)
                self._tick()
                next_tick += self._tick_interval_s
                sleep_for = next_tick - time.monotonic()
                if sleep_for < 0:
                    # Fell behind — skip ahead, don't burn CPU.
                    next_tick = time.monotonic()
                    sleep_for = 0.0
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
        finally:
            self._bus.unsubscribe("window.foreground", win_q)
            self._bus.unsubscribe("cpu.spike", spike_q)
            self._bus.unsubscribe("neuro.dopamine", dopa_q)
            log.info("reflex.mouse.cortex.stop")
