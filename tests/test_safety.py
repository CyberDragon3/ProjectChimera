"""Tests for the protected-species safety gate."""

from __future__ import annotations

import os

from chimera.safety import ProtectedSpecies


def test_whitelist_match_is_case_insensitive() -> None:
    s = ProtectedSpecies.from_list(["explorer.exe"])
    assert s.is_protected("EXPLORER.EXE")
    assert s.is_protected("explorer.exe")


def test_non_member_is_not_protected() -> None:
    s = ProtectedSpecies.from_list(["explorer.exe"])
    assert not s.is_protected("stress.exe")


def test_self_pid_always_protected() -> None:
    s = ProtectedSpecies.from_list([])
    assert s.is_protected("whatever.exe", pid=os.getpid())


def test_gate_denies_protected_and_allows_others(caplog) -> None:
    s = ProtectedSpecies.from_list(["explorer.exe"])
    assert not s.gate("explorer.exe", action="kill")
    assert s.gate("stress.exe", action="kill")


def test_members_returns_immutable_frozenset() -> None:
    s = ProtectedSpecies.from_list(["explorer.exe", "python.exe"])
    assert isinstance(s.members, frozenset)
    # User-configured members plus hardcoded kernel pseudo-processes.
    assert "explorer.exe" in s.members
    assert "python.exe" in s.members
    assert "system idle process" in s.members  # always hardcoded


def test_pid_zero_and_four_always_protected() -> None:
    s = ProtectedSpecies.from_list([])
    # PID 0 (System Idle) and PID 4 (System) must never be throttleable.
    assert s.is_protected("anything.exe", pid=0)
    assert s.is_protected("anything.exe", pid=4)
