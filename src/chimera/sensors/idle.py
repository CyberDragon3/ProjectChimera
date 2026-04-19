"""User-input idle sensor — Fly tier."""

from __future__ import annotations

import asyncio
import sys
import time

import structlog

from chimera.bus import Bus, Event
from chimera.sensors.base import IdleBackend

log = structlog.get_logger(__name__)


class Win32IdleBackend:
    """Uses GetLastInputInfo — constant-time, no admin required."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32IdleBackend only works on Windows")
        import ctypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        class LASTINPUTINFO(ctypes.Structure):  # type: ignore[misc]
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        self._lii_cls = LASTINPUTINFO

    def idle_seconds(self) -> float:
        lii = self._lii_cls()
        lii.cbSize = self._ctypes.sizeof(self._lii_cls)
        if not self._user32.GetLastInputInfo(self._ctypes.byref(lii)):
            return 0.0
        tick = self._kernel32.GetTickCount()
        return max(0.0, (tick - lii.dwTime) / 1000.0)


class NullIdleBackend:
    """Non-Windows fallback: never idle."""

    def idle_seconds(self) -> float:
        return 0.0


def make_default_idle_backend() -> IdleBackend:
    if sys.platform == "win32":
        try:
            return Win32IdleBackend()
        except Exception:  # pragma: no cover — defensive
            log.warning("sensor.idle.win32_unavailable")
    return NullIdleBackend()


class IdleSensor:
    """Polls idle time; publishes idle.enter / idle.exit state transitions."""

    def __init__(
        self,
        bus: Bus,
        backend: IdleBackend,
        interval_ms: int,
        idle_threshold_seconds: float,
    ) -> None:
        self._bus = bus
        self._backend = backend
        self._interval = interval_ms / 1000.0
        self._threshold = idle_threshold_seconds
        self._is_idle = False

    async def run(self) -> None:
        log.info("sensor.idle.start", threshold_s=self._threshold)
        while True:
            seconds = self._backend.idle_seconds()
            if seconds >= self._threshold and not self._is_idle:
                self._is_idle = True
                self._bus.publish(
                    Event(
                        topic="idle.enter",
                        payload={"seconds": seconds},
                        ts=time.monotonic(),
                    )
                )
            elif seconds < self._threshold and self._is_idle:
                self._is_idle = False
                self._bus.publish(
                    Event(
                        topic="idle.exit",
                        payload={"seconds": seconds},
                        ts=time.monotonic(),
                    )
                )
            await asyncio.sleep(self._interval)
