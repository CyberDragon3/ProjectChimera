"""Asyncio daemon entry point. Supervises sensors, reflexes, and bus."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

import structlog

from chimera.bus import Bus, Event
from chimera.config import Settings
from chimera.neuro.dopamine import DopamineModulator
from chimera.reflexes.fly import FlyNeuroReflex, FlyReflex
from chimera.reflexes.lysosome import LysosomeReflex, make_default_lysosome_backend
from chimera.reflexes.mouse import MouseCortex, MouseReflex
from chimera.reflexes.openworm import OpenWormDrive, OpenWormReflex
from chimera.reflexes.worm import PsutilThrottler, WormReflex
from chimera.reflexes.zebrafish import ZebrafishNeuroReflex, ZebrafishReflex
from chimera.safety import ProtectedSpecies
from chimera.sensors.cpu import CpuSensor, PsutilCpuBackend
from chimera.sensors.idle import IdleSensor, make_default_idle_backend
from chimera.sensors.thermal import ThermalSensor, make_default_thermal_backend
from chimera.sensors.window import WindowSensor, make_default_window_backend
from chimera.store import RingBuffer

log = structlog.get_logger(__name__)


class Chimera:
    """Root supervisor. Starts sensors and reflexes as asyncio tasks."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = Bus()
        self.safety = ProtectedSpecies.from_list(settings.protected_species.processes)
        self.thermal_buf = RingBuffer(max_seconds=settings.store.ring_buffer_seconds)
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def spawn(self, name: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
        async def _runner() -> None:
            log.info("task.start", task=name)
            try:
                await coro_factory()
            except asyncio.CancelledError:
                log.info("task.cancelled", task=name)
                raise
            except Exception:
                log.exception("task.error", task=name)
                # A supervised task crashing is treated as fatal — set the stop
                # event so the daemon shuts down cleanly instead of silently
                # losing a sensor or reflex with no auto-restart.
                self._stop.set()
                raise

        self._tasks.append(asyncio.create_task(_runner(), name=name))

    async def _heartbeat(self) -> None:
        """Periodic JSON heartbeat for dry-run observability."""
        while not self._stop.is_set():
            self.bus.publish(
                Event(
                    topic="chimera.heartbeat",
                    payload={"dropped": self.bus.dropped},
                    ts=time.monotonic(),
                )
            )
            log.info(
                "heartbeat",
                tasks=len(self._tasks),
                protected_count=len(self.safety.members),
                bus_dropped=self.bus.dropped,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def run(self, dry_run: bool = False) -> None:
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            # Always hop back to the loop thread before touching the event.
            loop.call_soon_threadsafe(self._stop.set)

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                # Windows lacks add_signal_handler for some signals; the C
                # signal handler runs on the main thread but outside the loop.
                signal.signal(sig, lambda *_: _request_stop())

        log.info("chimera.start", dry_run=dry_run, protected=len(self.safety.members))

        self.spawn("heartbeat", self._heartbeat)
        if not dry_run:
            self._start_reflexes()
            if self.settings.dashboard.enabled:
                self._start_dashboard()

        await self._stop.wait()
        log.info("chimera.stopping")

        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with suppress(asyncio.CancelledError):
                await t

        log.info("chimera.stopped")

    def _start_reflexes(self) -> None:
        """Wire up all sensors and reflexes as supervised asyncio tasks."""
        s = self.settings

        # Tier-3 sensors.
        cpu_sensor = CpuSensor(
            self.bus,
            PsutilCpuBackend(),
            interval_ms=s.poll.cpu_interval_ms,
            spike_percent=s.thresholds.cpu_spike_percent,
        )
        idle_sensor = IdleSensor(
            self.bus,
            make_default_idle_backend(),
            interval_ms=s.poll.idle_interval_ms,
            idle_threshold_seconds=s.thresholds.idle_seconds,
        )
        # Tier-2 sensors.
        window_sensor = WindowSensor(
            self.bus,
            make_default_window_backend(),
            interval_ms=s.poll.window_interval_ms,
        )
        thermal_sensor = ThermalSensor(
            self.bus,
            make_default_thermal_backend(),
            self.thermal_buf,
            interval_ms=s.poll.thermal_interval_ms,
        )

        # Reflexes.
        openworm_drive = OpenWormDrive()
        worm = WormReflex(
            self.bus,
            self.safety,
            PsutilThrottler(),
            openworm=openworm_drive,
            deadline_ms=s.thresholds.reflex_deadline_ms,
        )
        openworm = OpenWormReflex(self.bus)
        if s.neuro.enabled:
            fly = FlyNeuroReflex(self.bus, neuro_cfg=s.neuro)
            zebrafish = ZebrafishNeuroReflex(
                self.bus,
                self.thermal_buf,
                neuro_cfg=s.neuro,
                slope_c_per_min_threshold=s.thresholds.thermal_slope_c_per_min,
                critical_c=s.thresholds.thermal_critical_c,
                critical_clear_c=s.thresholds.thermal_critical_clear_c,
                critical_samples=s.thresholds.thermal_critical_samples,
                max_hold_seconds=s.thresholds.thermal_critical_max_hold_seconds,
            )
            mouse = MouseCortex(self.bus, neuro_cfg=s.neuro)
        else:
            fly = FlyReflex(self.bus)  # type: ignore[assignment]
            zebrafish = ZebrafishReflex(  # type: ignore[assignment]
                self.bus,
                self.thermal_buf,
                slope_c_per_min_threshold=s.thresholds.thermal_slope_c_per_min,
                critical_c=s.thresholds.thermal_critical_c,
                critical_clear_c=s.thresholds.thermal_critical_clear_c,
                critical_samples=s.thresholds.thermal_critical_samples,
                max_hold_seconds=s.thresholds.thermal_critical_max_hold_seconds,
                interval_ms=s.poll.thermal_interval_ms,
            )
            mouse = MouseReflex(self.bus)  # type: ignore[assignment]
        lysosome = LysosomeReflex(
            self.bus,
            self.safety,
            make_default_lysosome_backend(),
            enabled=s.lysosome.enabled,
            sweep_interval_seconds=s.lysosome.sweep_interval_seconds,
            targets=s.lysosome.targets,
        )

        for name, obj in [
            ("sensor.cpu", cpu_sensor),
            ("sensor.idle", idle_sensor),
            ("sensor.window", window_sensor),
            ("sensor.thermal", thermal_sensor),
            ("reflex.worm", worm),
            ("reflex.openworm", openworm),
            ("reflex.fly", fly),
            ("reflex.zebrafish", zebrafish),
            ("reflex.mouse", mouse),
            ("reflex.lysosome", lysosome),
        ]:
            self.spawn(name, obj.run)  # type: ignore[attr-defined]

        if s.neuro.enabled:
            dopamine = DopamineModulator(self.bus)
            self.spawn("reflex.neuro.dopamine", lambda: dopamine.run(self._stop))

    def _start_dashboard(self) -> None:
        s = self.settings

        async def _serve() -> None:
            import uvicorn  # type: ignore[import-not-found]

            from chimera.dashboard.app import create_app

            try:
                from chimera.brains import BRAINS_AVAILABLE  # type: ignore[import-not-found]
                openworm_state = OpenWormReflex(self.bus).initial_state()
            except Exception:
                BRAINS_AVAILABLE = {
                    "psutil": True,
                    "owmeta": False,
                    "bmtk": False,
                    "flygym": False,
                }
                openworm_state = None

            runtime_flags = {
                "neuro_enabled": bool(s.neuro.enabled),
                "lysosome_enabled": bool(s.lysosome.enabled),
                "dashboard_enabled": bool(s.dashboard.enabled),
            }

            app = create_app(
                self.bus,
                self.thermal_buf,
                protected_species=frozenset(self.safety.members),
                brains_available=BRAINS_AVAILABLE,
                runtime_flags=runtime_flags,
                worm_state=openworm_state,
            )
            config = uvicorn.Config(
                app,
                host=s.dashboard.host,
                port=s.dashboard.port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            # Don't let uvicorn replace our signal handlers on the main loop.
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            await server.serve()

        self.spawn("dashboard", _serve)
