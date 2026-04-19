"""Thermal sensor — Zebrafish tier.

Queries LibreHardwareMonitor's WMI namespace for CPU/GPU temperatures.
Requires the LHM service to be running as admin (see scripts/install_lhm.ps1).
Non-Windows falls back to a null backend that returns None.
"""

from __future__ import annotations

import asyncio
import sys
import time

import structlog

from chimera.bus import Bus, Event
from chimera.sensors.base import ThermalBackend
from chimera.store import RingBuffer

log = structlog.get_logger(__name__)


class LhmThermalBackend:
    """LibreHardwareMonitor WMI query. Averages all Temperature sensors."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("LhmThermalBackend requires Windows")
        import wmi  # type: ignore[import-not-found]

        self._wmi = wmi.WMI(namespace=r"root\LibreHardwareMonitor")

    def read_celsius(self) -> float | None:
        try:
            sensors = [
                s for s in self._wmi.Sensor() if getattr(s, "SensorType", "") == "Temperature"
            ]
        except Exception as e:  # pragma: no cover — depends on LHM availability
            log.warning("sensor.thermal.query_failed", error=str(e))
            return None
        if not sensors:
            return None
        # WMI may return non-numeric strings (e.g. "N/A"); coerce defensively
        # so a single bad sensor cannot kill the polling task.
        values: list[float] = []
        for s in sensors:
            v = getattr(s, "Value", None)
            if v is None:
                continue
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                continue
        if not values:
            return None
        # Report the hottest reading — most actionable signal.
        return max(values)


class NullThermalBackend:
    def read_celsius(self) -> float | None:
        return None


def make_default_thermal_backend() -> ThermalBackend:
    if sys.platform != "win32":
        return NullThermalBackend()
    try:
        return LhmThermalBackend()
    except Exception as e:
        msg = str(e).lower()
        if "invalid namespace" in msg or "0x8004100e" in msg or "2147217394" in msg:
            log.warning(
                "sensor.thermal.lhm_service_missing",
                error=str(e),
                remediation=(
                    "LibreHardwareMonitor WMI namespace not found. "
                    "Install + run LHM as admin: scripts/install_lhm.ps1"
                ),
            )
        else:
            log.warning("sensor.thermal.unavailable", error=str(e))
    return NullThermalBackend()


class ThermalSensor:
    """Polls a ThermalBackend, appends to a shared RingBuffer, publishes readings."""

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
        log.info("sensor.thermal.start", interval_ms=int(self._interval * 1000))
        while True:
            try:
                # WMI calls routinely take 50–500 ms; offload from the loop.
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
