"""Lysosome scavenger — backend contract + sweep behavior."""
from __future__ import annotations

from chimera.reflexes.lysosome import LysosomeBackend, NullLysosomeBackend


def test_null_backend_is_noop():
    b: LysosomeBackend = NullLysosomeBackend()
    assert b.trim_working_set([1, 2, 3]) == 0
    assert b.flush_system_cache() is None
    assert b.kill(1234) is False
