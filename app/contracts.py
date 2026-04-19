"""Frozen data contracts shared across tiers.

Every subagent codes against these. DO NOT modify field names/types without
updating all dependents. Add new fields with defaults if you must extend.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

import numpy as np

Module = Literal["fly", "worm", "mouse", "executive", "system"]


@dataclass
class FlyPolicy:
    sensitivity: float = 0.5
    looming_threshold: float = 0.35


@dataclass
class WormPolicy:
    cpu_pain_threshold: float = 0.85
    ram_pain_threshold: float = 0.90
    # 0.80 matches the bundled config.yaml default — keeps the reflex from
    # twitching on ordinary psutil jitter (typical d(pressure)/dt ~ 0.1-0.3
    # during normal workstation use).
    poke_derivative: float = 0.80
    dwell_ms: int = 800


@dataclass
class MousePolicy:
    track_target_xy: Optional[tuple[int, int]] = None
    error_threshold: float = 120.0
    consecutive_frames: int = 3


@dataclass
class BioPolicy:
    fly: FlyPolicy = field(default_factory=FlyPolicy)
    worm: WormPolicy = field(default_factory=WormPolicy)
    mouse: MousePolicy = field(default_factory=MousePolicy)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["mouse"]["track_target_xy"] is not None:
            d["mouse"]["track_target_xy"] = list(d["mouse"]["track_target_xy"])
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BioPolicy":
        fly = FlyPolicy(**(d.get("fly") or {}))
        worm = WormPolicy(**(d.get("worm") or {}))
        m = dict(d.get("mouse") or {})
        if m.get("track_target_xy") is not None:
            m["track_target_xy"] = tuple(m["track_target_xy"])
        mouse = MousePolicy(**m)
        return cls(fly=fly, worm=worm, mouse=mouse)


@dataclass
class OmmatidiaFrame:
    """Downsampled screen luminance + temporal diff grid."""
    t_ns: int
    luminance: np.ndarray       # shape (grid, grid), dtype float32, 0..1
    diff: np.ndarray            # shape (grid, grid), dtype float32, signed


@dataclass
class PressureSample:
    """Somatosensory pressure (CPU/RAM fused) at a point in time."""
    t_ns: int
    cpu: float                  # 0..1
    ram: float                  # 0..1
    pressure: float             # fused 0..1
    derivative: float           # d(pressure)/dt in 1/s


@dataclass
class CursorSample:
    t_ns: int
    x: int
    y: int
    vx: float = 0.0
    vy: float = 0.0


@dataclass
class InterruptEvent:
    """Reflex fire. `t_stimulus_ns` is when the causing stimulus was sampled;
    `t_fire_ns` is when the connectome decided; `t_action_ns` is set by the
    main loop after the action handler returns."""
    module: Module
    kind: str                   # e.g. "looming", "ava_recoil", "error_spike"
    payload: dict[str, Any] = field(default_factory=dict)
    t_stimulus_ns: int = 0
    t_fire_ns: int = 0
    t_action_ns: int = 0

    def latency_us(self) -> float:
        if self.t_action_ns and self.t_stimulus_ns:
            return (self.t_action_ns - self.t_stimulus_ns) / 1_000.0
        return 0.0


@dataclass
class ExecutiveEvent:
    """Anything the LLM layer emits to the UI."""
    t_ns: int
    kind: Literal["prompt", "policy", "explain", "status", "error", "shell_output"]
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
