"""CPU sensor — polls psutil, publishes cpu.spike events."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

import psutil
import structlog

from chimera.bus import Bus, Event
from chimera.sensors.base import CpuBackend, ProcSample

log = structlog.get_logger(__name__)


class PsutilCpuBackend:
    """Real CPU backend. Primes cpu_percent lazily on first sighting of each pid.

    A hot-path ``iter_process_samples`` takes ~30–80 ms on a typical Win11 box
    (see research notes). Callers should invoke it inside ``asyncio.to_thread``
    to avoid blocking the event loop.
    """

    def __init__(self) -> None:
        self._primed: dict[int, psutil.Process] = {}

    def iter_process_samples(self) -> Iterable[ProcSample]:
        current = set()
        for p in psutil.process_iter(["pid", "name"]):
            current.add(p.pid)
            try:
                if p.pid not in self._primed:
                    p.cpu_percent(None)
                    self._primed[p.pid] = p
                    continue
                cached = self._primed[p.pid]
                pct = cached.cpu_percent(None)
                info = p.info
                exe = info.get("name") or ""
                try:
                    rss = cached.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    rss = 0
                yield ProcSample(pid=p.pid, exe=exe, cpu_percent=pct, rss_bytes=rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Drop dead pids.
        for dead in [pid for pid in self._primed if pid not in current]:
            self._primed.pop(dead, None)


class CpuSensor:
    """Polls a CpuBackend at the configured interval and publishes bus events."""

    def __init__(
        self,
        bus: Bus,
        backend: CpuBackend,
        interval_ms: int,
        spike_percent: float,
    ) -> None:
        self._bus = bus
        self._backend = backend
        self._interval = interval_ms / 1000.0
        self._spike = spike_percent

    def _scan(self) -> list[ProcSample]:
        return [s for s in self._backend.iter_process_samples() if s.cpu_percent >= self._spike]

    async def run(self) -> None:
        log.info("sensor.cpu.start", interval_ms=int(self._interval * 1000))
        while True:
            t0 = time.monotonic()
            # psutil is a blocking C call; run in a thread so we don't starve
            # the dashboard / reflex tasks on the same event loop.
            spikes = await asyncio.to_thread(self._scan)
            for sample in spikes:
                self._bus.publish(
                    Event(
                        topic="cpu.spike",
                        payload={
                            "pid": sample.pid,
                            "exe": sample.exe,
                            "cpu_percent": sample.cpu_percent,
                            "rss_bytes": sample.rss_bytes,
                        },
                        ts=time.monotonic(),
                    )
                )
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, self._interval - elapsed))
