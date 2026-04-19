"""Protected-species safety gate.

Deny-by-default whitelist. Loaded once at boot into a frozenset; not
mutable at runtime. Every destructive action MUST pass through
``is_protected()`` before being executed.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProtectInfo:
    exe: str
    reason: str


class ProtectedSpecies:
    """Immutable whitelist of processes that must never be killed/throttled hard."""

    # Windows pseudo-processes that report absurd CPU% but are not real procs.
    # PID 0 = System Idle Process, PID 4 = System. Kernel-space, never touchable.
    _HARDCODED_PIDS: frozenset[int] = frozenset({0, 4})
    _HARDCODED_EXES: frozenset[str] = frozenset({"system idle process", "system"})

    def __init__(self, processes: frozenset[str]) -> None:
        self._processes = frozenset(p.lower() for p in processes) | self._HARDCODED_EXES
        self._self_pid = os.getpid()

    @classmethod
    def from_list(cls, processes: Iterable[str]) -> ProtectedSpecies:
        return cls(frozenset(processes))

    def is_protected(self, exe_name: str, pid: int | None = None) -> bool:
        """Return True if the process is on the whitelist or is us."""
        if pid is not None and (pid == self._self_pid or pid in self._HARDCODED_PIDS):
            return True
        return exe_name.lower() in self._processes

    def gate(
        self,
        exe_name: str,
        action: str,
        pid: int | None = None,
    ) -> bool:
        """Check and log. Returns True if action is ALLOWED."""
        if self.is_protected(exe_name, pid=pid):
            log.warning(
                "safety.denied",
                exe=exe_name,
                pid=pid,
                action=action,
                reason="protected_species",
            )
            return False
        return True

    @property
    def members(self) -> frozenset[str]:
        return self._processes
