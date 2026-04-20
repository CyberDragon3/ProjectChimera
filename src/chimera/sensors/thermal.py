"""
Synthetic Thermal Sensor — Zebrafish tier.
Uses a mathematical model to estimate CPU temperature based on load.
Bypasses the need for Admin privileges and WMI overhead.
"""

from __future__ import annotations

import asyncio
import time
import psutil
import structlog

from chimera.bus import Bus, Event
from chimera.sensors.base import ThermalBackend
from chimera.store import RingBuffer

log = structlog.get_logger(__name__)

class SyntheticThermalBackend:
    """
    Simulates thermal dynamics without hitting hardware registers.
    Uses a non-linear scaling: Heat increases exponentially with load.
    """

    def __init__(self) -> None:
        # Pre-warm psutil to avoid a 0.0 first reading
        psutil.cpu_percent(interval=None)
        self._last_temp = 42.0  # Initial idle temp

    def read_celsius(self) -> float | None:
        try:
            # 1. Get current load (non-blocking)
            load = psutil.cpu_percent(interval=None)
            
            # 2. Thermal Model Parameters
            # Base: 40°C (Idle) | Max: 85°C (Full Load)
            ambient = 40.0
            thermal_range = 45.0
            
            # 3. Calculate target temp using a power curve (0.8 exponent)
            # This makes the temp rise quickly at first, then level out—just like real silicon.
            target = ambient + (thermal_range * (load / 100.0)**0.8)
            
            # 4. Thermal Inertia (Simple Low-Pass Filter)
            # Silicon doesn't jump 40 degrees in 1 millisecond. 
            # We move the 'current' temp 15% toward the 'target' per poll.
            smoothing = 0.15
            new_temp = self._last_temp + (smoothing * (target - self._last_temp))
            
            self._last_temp = new_temp
            return round(new_temp, 1)
            
        except Exception as e:
            log.warning("sensor.thermal.synthetic_failed", error=str(e))
            return None

def make_default_thermal_backend() -> ThermalBackend:
    """Always returns the Synthetic backend to ensure zero-permission dashboarding."""
    return SyntheticThermalBackend()

class ThermalSensor:
    """Polls the backend and pushes to the bus. Identical to original for compatibility."""

    def __init__(
        self,
        bus: Bus,
        backend: ThermalBackend,
        buffer: RingBuffer,
        interval_ms: int,
    ) -> None:
        self._bus = bus
        self._backend = backend
        self._buf = buffer
        self._interval = interval_ms / 1000.0

    async def run(self) -> None:
        log.info("sensor.thermal.start_synthetic", interval_ms=int(self._interval * 1000))
        while True:
            try:
                # Synthetic is fast enough to run in-thread, but we keep to_thread for architectural parity.
                c = await asyncio.to_thread(self._backend.read_celsius)
                if c is not None:
                    self._buf.append(c)
                    self._bus.publish(
                        Event(topic="thermal.sample", payload={"celsius": c}, ts=time.monotonic())
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("sensor.thermal.iteration_failed", error=str(e))
            await asyncio.sleep(self._interval)