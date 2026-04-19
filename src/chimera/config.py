"""TOML-backed configuration via pydantic-settings."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtectedSpeciesCfg(_Frozen):
    processes: tuple[str, ...] = ()


class Thresholds(_Frozen):
    cpu_spike_percent: float = 85.0
    cpu_sustained_seconds: float = 1.0
    thermal_slope_c_per_min: float = 2.5
    idle_seconds: int = 300
    reflex_deadline_ms: int = 10
    thermal_critical_c: float = 95.0
    thermal_critical_clear_c: float = 90.0
    thermal_critical_samples: int = 2
    thermal_critical_max_hold_seconds: int = 300


class Poll(_Frozen):
    cpu_interval_ms: int = 250
    idle_interval_ms: int = 1000
    thermal_interval_ms: int = 5000
    window_interval_ms: int = 1000


class Store(_Frozen):
    ring_buffer_seconds: int = 300


class LlmCfg(_Frozen):
    enabled: bool = False
    provider: Literal["ollama", "claude"] = "ollama"
    model: str = "phi3:mini"
    claude_model: str = "claude-haiku-4-5"
    min_interval_seconds: float = 30.0
    max_daily_calls: int = 500


class Dashboard(_Frozen):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


class Logging(_Frozen):
    level: str = "INFO"
    format: Literal["json", "console"] = "json"


class LysosomeCfg(_Frozen):
    enabled: bool = True
    sweep_interval_seconds: int = 600
    targets: tuple[str, ...] = ()


class Settings(_Frozen):
    protected_species: ProtectedSpeciesCfg = Field(default_factory=ProtectedSpeciesCfg)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    poll: Poll = Field(default_factory=Poll)
    store: Store = Field(default_factory=Store)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    dashboard: Dashboard = Field(default_factory=Dashboard)
    logging: Logging = Field(default_factory=Logging)
    lysosome: LysosomeCfg = Field(default_factory=LysosomeCfg)


def load(path: Path) -> Settings:
    """Load and freeze a Settings object from a TOML file."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)


def load_default() -> Settings:
    """Load config/chimera.toml from the repo root, with a local override if present."""
    root = Path(__file__).resolve().parents[2]
    base = root / "config" / "chimera.toml"
    local = root / "config" / "chimera.local.toml"
    path = local if local.exists() else base
    return load(path)
