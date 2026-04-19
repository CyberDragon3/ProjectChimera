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

    # Windows kernel pseudo-processes that report ~1400% CPU. Filter at the
    # sensor edge so reflexes never see them, even if the safety gate would.
    _PSEUDO_PIDS: frozenset[int] = frozenset({0, 4})
    _PSEUDO_NAMES: frozenset[str] = frozenset({"system idle process", "system"})

    def __init__(self) -> None:
        # (pid -> (Process, create_time)) so we can detect pid recycling.
        self._primed: dict[int, tuple[psutil.Process, float]] = {}

    def iter_process_samples(self) -> Iterable[ProcSample]:
        current = set()
        for p in psutil.process_iter(["pid", "name"]):
            pid = p.pid
            current.add(pid)
            info = p.info
            name = (info.get("name") or "").lower()
            if pid in self._PSEUDO_PIDS or name in self._PSEUDO_NAMES:
                continue
            try:
                ctime = p.create_time()
                cached_entry = self._primed.get(pid)
                # Re-prime if pid is new or has been recycled to a new process.
                if cached_entry is None or cached_entry[1] != ctime:
                    p.cpu_percent(None)
                    self._primed[pid] = (p, ctime)
                    continue
                cached, _ = cached_entry
                pct = cached.cpu_percent(None)
                exe = info.get("name") or ""
                try:
                    rss = cached.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    rss = 0
                yield ProcSample(pid=pid, exe=exe, cpu_percent=pct, rss_bytes=rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OSError):
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
            try:
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
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A transient psutil/COM error must not kill the supervised task.
                log.warning("sensor.cpu.iteration_failed", error=str(e))
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, self._interval - elapsed))
