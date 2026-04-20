"""OpenWorm-backed worm activity and modulation helpers."""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

import structlog

from chimera.brains.openworm_shards import bundle_summary
from chimera.bus import Bus, Event

log = structlog.get_logger(__name__)


class OpenWormDrive:
    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        neuron_names: tuple[str, ...] | None = None,
    ) -> None:
        if neuron_names is None:
            summary = bundle_summary(base_dir)
            self._graphs_dir = str(summary["graphs_dir"])
            self._neuron_names = tuple(str(name) for name in summary["neuron_names"])
        else:
            self._graphs_dir = None
            self._neuron_names = tuple(neuron_names)
        self.available = bool(self._neuron_names)

    @property
    def neuron_count(self) -> int:
        return len(self._neuron_names)

    def initial_state(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "neuron_count": self.neuron_count,
            "active_count": 0,
            "active_fraction": 0.0,
            "status": "idle",
            "sample": list(self._neuron_names[:10]),
            "active_neurons": [],
            "graphs_dir": self._graphs_dir,
        }

    def _select_neurons(self, seed: str, count: int) -> tuple[str, ...]:
        if not self._neuron_names:
            return ()
        rng = random.Random(seed)
        size = min(len(self._neuron_names), max(1, count))
        selected = rng.sample(list(self._neuron_names), size)
        return tuple(sorted(selected))

    def model_state(
        self,
        status: str,
        *,
        pid: int,
        exe: str,
        cpu_percent: float,
    ) -> dict[str, Any]:
        if not self.available:
            return self.initial_state() | {
                "status": status,
                "pid": pid,
                "exe": exe,
                "cpu_percent": cpu_percent,
                "confidence": 0.0,
                "throttle_level": None,
            }
        scale = 36 if status == "throttle" else 18
        minimum = 6 if status == "throttle" else 1
        drive = max(minimum, round(min(1.0, max(0.0, cpu_percent) / 100.0) * scale))
        active = self._select_neurons(f"{status}:{pid}:{exe}:{round(cpu_percent, 1)}", drive)
        active_count = len(active)
        active_fraction = (active_count / self.neuron_count) if self.neuron_count else 0.0
        confidence = min(1.0, 0.55 * active_fraction + 0.45 * min(1.0, cpu_percent / 100.0))
        throttle_level = "idle" if status == "throttle" or confidence >= 0.68 else "below_normal"
        return {
            "available": self.available,
            "neuron_count": self.neuron_count,
            "active_count": active_count,
            "active_fraction": active_fraction,
            "status": status,
            "sample": list(self._neuron_names[:10]),
            "active_neurons": list(active[:12]),
            "graphs_dir": self._graphs_dir,
            "pid": pid,
            "exe": exe,
            "cpu_percent": cpu_percent,
            "confidence": confidence,
            "throttle_level": throttle_level,
        }


class OpenWormReflex:
    def __init__(
        self,
        bus: Bus,
        *,
        base_dir: Path | None = None,
        neuron_names: tuple[str, ...] | None = None,
    ) -> None:
        self._bus = bus
        self._drive = OpenWormDrive(base_dir=base_dir, neuron_names=neuron_names)
        self.available = self._drive.available
        self._graphs_dir = self._drive._graphs_dir

    @property
    def neuron_count(self) -> int:
        return self._drive.neuron_count

    def initial_state(self) -> dict[str, Any]:
        return self._drive.initial_state()

    def _emit_state(self, state: dict[str, Any], ts: float) -> None:
        self._bus.publish(Event(topic="neuro.worm.state", payload=state, ts=ts))

    def _handle_cpu_spike(self, event: Event) -> None:
        if not self.available:
            return
        exe = str(event.payload.get("exe", ""))
        pid = int(event.payload.get("pid", -1))
        cpu_percent = float(event.payload.get("cpu_percent", 0.0))
        self._emit_state(
            self._drive.model_state("spike", pid=pid, exe=exe, cpu_percent=cpu_percent),
            time.monotonic(),
        )

    def _handle_throttle(self, event: Event) -> None:
        if not self.available:
            return
        exe = str(event.payload.get("exe", ""))
        pid = int(event.payload.get("pid", -1))
        cpu_percent = float(event.payload.get("cpu_percent", 0.0))
        self._emit_state(
            self._drive.model_state("throttle", pid=pid, exe=exe, cpu_percent=cpu_percent),
            time.monotonic(),
        )

    async def run(self) -> None:
        if not self.available:
            log.info("reflex.openworm.skip", reason="bundle_unavailable")
            return

        spike_q = self._bus.subscribe("cpu.spike")
        throttle_q = self._bus.subscribe("reflex.worm.throttle")
        log.info(
            "reflex.openworm.start",
            neuron_count=self.neuron_count,
            graphs_dir=self._graphs_dir,
        )
        self._bus.publish(
            Event(topic="neuro.worm.state", payload=self.initial_state(), ts=time.monotonic())
        )
        spike_task: asyncio.Task[Event] = asyncio.create_task(spike_q.get())
        throttle_task: asyncio.Task[Event] = asyncio.create_task(throttle_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {spike_task, throttle_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    event = task.result()
                    if task is spike_task:
                        spike_task = asyncio.create_task(spike_q.get())
                        self._handle_cpu_spike(event)
                    else:
                        throttle_task = asyncio.create_task(throttle_q.get())
                        self._handle_throttle(event)
        finally:
            for pending in (spike_task, throttle_task):
                if not pending.done():
                    pending.cancel()
            self._bus.unsubscribe("cpu.spike", spike_q)
            self._bus.unsubscribe("reflex.worm.throttle", throttle_q)
