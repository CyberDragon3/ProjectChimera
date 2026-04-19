"""Opt-in adapters for real-biology frameworks (owmeta, bmtk, flygym).

Each adapter detects whether its framework is importable and exposes a
best-effort summary. The daemon never calls these at reflex time — they are
for data loading, offline calibration, and dashboard visualization.
"""
from __future__ import annotations

from chimera.brains.bmtk_adapter import BmtkAdapter
from chimera.brains.flygym_adapter import FlygymAdapter
from chimera.brains.owmeta_adapter import OwmetaAdapter

_owmeta = OwmetaAdapter()
_bmtk = BmtkAdapter()
_flygym = FlygymAdapter()

BRAINS_AVAILABLE: dict[str, bool] = {
    "psutil": True,   # mandatory dep — always present
    "owmeta": _owmeta.available,
    "bmtk": _bmtk.available,
    "flygym": _flygym.available,
}


def summary() -> dict[str, dict[str, object]]:
    """Full adapter status payload for the dashboard."""
    return {
        "owmeta": _owmeta.info(),
        "bmtk": _bmtk.info(),
        "flygym": _flygym.info(),
    }


__all__ = ["BRAINS_AVAILABLE", "BmtkAdapter", "FlygymAdapter", "OwmetaAdapter", "summary"]
