"""Tests for TOML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from chimera.config import Settings, load, load_default


def test_load_default_config() -> None:
    s = load_default()
    assert isinstance(s, Settings)
    assert "explorer.exe" in s.protected_species.processes
    assert s.thresholds.cpu_spike_percent > 0
    assert s.poll.cpu_interval_ms >= 100


def test_config_is_frozen() -> None:
    s = load_default()
    with pytest.raises((TypeError, ValueError)):
        s.thresholds.cpu_spike_percent = 50.0  # type: ignore[misc]


def test_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[nonsense]\nkey = 1\n")
    with pytest.raises(Exception):
        load(bad)
