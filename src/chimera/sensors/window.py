"""Active-window sensor — Mouse tier context."""

from __future__ import annotations

import asyncio
import sys
import time

import psutil
import structlog

from chimera.bus import Bus, Event
from chimera.sensors.base import WindowBackend

log = structlog.get_logger(__name__)


class Win32WindowBackend:
    """GetForegroundWindow + GetWindowText + PID-to-exe lookup via psutil."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32WindowBackend requires Windows")
        import win32gui  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]

        self._gui = win32gui
        self._proc = win32process

    def foreground(self) -> tuple[str, str]:
        hwnd = self._gui.GetForegroundWindow()
        if not hwnd:
            return ("", "")
        title = self._gui.GetWindowText(hwnd) or ""
        try:
            _, pid = self._proc.GetWindowThreadProcessId(hwnd)
            exe = psutil.Process(pid).name() if pid else ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            exe = ""
        return (exe, title)


class NullWindowBackend:
    def foreground(self) -> tuple[str, str]:
        return ("", "")


def make_default_window_backend() -> WindowBackend:
    if sys.platform == "win32":
        try:
            return Win32WindowBackend()
        except Exception as e:
            log.warning("sensor.window.unavailable", error=str(e))
    return NullWindowBackend()


class WindowSensor:
    """Polls foreground; publishes on change only (cheap, low noise)."""

    def __init__(self, bus: Bus, backend: WindowBackend, interval_ms: int) -> None:
        self._bus = bus
        self._backend = backend
        self._interval = interval_ms / 1000.0
        self._last: tuple[str, str] | None = None

    async def run(self) -> None:
        log.info("sensor.window.start", interval_ms=int(self._interval * 1000))
        while True:
            current = self._backend.foreground()
            if current != self._last and current[0]:
                self._last = current
                exe, title = current
                self._bus.publish(
                    Event(
                        topic="window.foreground",
                        payload={"exe": exe, "title": title},
                        ts=time.monotonic(),
                    )
                )
            await asyncio.sleep(self._interval)
