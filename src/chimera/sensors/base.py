"""Sensor protocol contracts so production Win32 calls can be mocked in tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True, frozen=True)
class ProcSample:
    pid: int
    exe: str
    cpu_percent: float
    rss_bytes: int


class CpuBackend(Protocol):
    def iter_process_samples(self) -> Iterable[ProcSample]: ...


class IdleBackend(Protocol):
    def idle_seconds(self) -> float: ...


class WindowBackend(Protocol):
    def foreground(self) -> tuple[str, str, int | None]:
        """Return (exe_name, window_title, pid)."""
        ...


class ThermalBackend(Protocol):
    def read_celsius(self) -> float | None: ...
