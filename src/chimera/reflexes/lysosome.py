"""Lysosome scavenger — idle-time cleanup reflex.

Three-phase sweep triggered on ``idle.enter`` (the Fly's Deep-Breath state):
  1. Working-set trim — non-destructive, OS re-pages on demand.
  2. System cache flush — admin-only; logs + skips on access-denied.
  3. Opt-in target kill — only exes in [lysosome] targets; safety-gated.

See design §5.5. Added to test_safety_audit's ALLOWED_MODULES in Phase 7
because phase 3 invokes ``proc.kill()`` after passing through ``safety.gate``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterable
from typing import Protocol

import structlog

from chimera.bus import Bus, Event
from chimera.safety import ProtectedSpecies

log = structlog.get_logger(__name__)


class LysosomeBackend(Protocol):
    def trim_working_set(self, pids: Iterable[int]) -> int: ...
    def flush_system_cache(self) -> int | None: ...
    def kill(self, pid: int) -> bool: ...


class NullLysosomeBackend:
    """Non-Windows / test fallback. All phases become no-ops."""

    def trim_working_set(self, pids: Iterable[int]) -> int:
        return 0

    def flush_system_cache(self) -> int | None:
        return None

    def kill(self, pid: int) -> bool:
        return False
