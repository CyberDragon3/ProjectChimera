"""Tests for TOML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

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


def test_settings_has_thermal_critical_defaults() -> None:
    s = Settings()
    assert s.thresholds.thermal_critical_c == 95.0
    assert s.thresholds.thermal_critical_clear_c == 90.0
    assert s.thresholds.thermal_critical_samples == 2
    assert s.thresholds.thermal_critical_max_hold_seconds == 300


def test_settings_has_lysosome_defaults() -> None:
    s = Settings()
    assert s.lysosome.enabled is True
    assert s.lysosome.sweep_interval_seconds == 600
    assert s.lysosome.targets == ()


def test_lysosome_targets_is_immutable() -> None:
    s = Settings(lysosome={"targets": ["foo.exe", "bar.exe"]})
    assert s.lysosome.targets == ("foo.exe", "bar.exe")
    with pytest.raises(ValidationError):
        s.lysosome.targets = ("evil.exe",)  # type: ignore[misc]


def test_load_default_toml_parses_new_keys(tmp_path: Path) -> None:
    toml = tmp_path / "c.toml"
    toml.write_text(
        "[thresholds]\n"
        "thermal_critical_c = 92.5\n"
        "[lysosome]\n"
        "enabled = false\n"
        'targets = ["chrome_crashpad_handler.exe"]\n'
    )
    s = load(toml)
    assert s.thresholds.thermal_critical_c == 92.5
    assert s.lysosome.enabled is False
    assert s.lysosome.targets == ("chrome_crashpad_handler.exe",)
