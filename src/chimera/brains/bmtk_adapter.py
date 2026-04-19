"""bmtk — Allen Institute's Brain Modeling Toolkit (GLIF cell library).

Adapter provides a static mapping from BMTK's GLIF model IDs to the
parameter surface our `NeuroCfg` expects. Also reports whether bmtk
is importable so the dashboard can show it.

Usage (offline):
  adapter = BmtkAdapter()
  params = adapter.glif_params_from_dict(some_bmtk_cell_json)
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Minimal Allen Cell Types GLIF-3 default — reasonable mouse cortical pyramidal.
_ALLEN_DEFAULT_GLIF3 = {
    "tau_m_ms": 22.0,
    "v_rest_mv": -70.0,
    "v_reset_mv": -75.0,
    "v_thresh_mv": -50.0,
    "refractory_ms": 3.0,
}


class BmtkAdapter:
    def __init__(self) -> None:
        self.available: bool = False
        self._error: str | None = None
        try:
            import bmtk  # type: ignore[import-not-found]  # noqa: F401

            self.available = True
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"

    @staticmethod
    def allen_default_glif3() -> dict[str, float]:
        return dict(_ALLEN_DEFAULT_GLIF3)

    def glif_params_from_dict(self, cell: dict[str, Any]) -> dict[str, float]:
        """Convert a BMTK GLIF cell dict into NeuroCfg-compatible params.

        BMTK stores GLIF params in a 'dynamics_params' dict. We accept either
        a raw dynamics_params dict or a full cell dict with that key.
        """
        dyn = cell.get("dynamics_params", cell)
        return {
            "tau_m_ms": float(dyn.get("tau_m", _ALLEN_DEFAULT_GLIF3["tau_m_ms"] / 1000.0)) * 1000.0,
            "v_rest_mv": float(dyn.get("V_reset", _ALLEN_DEFAULT_GLIF3["v_reset_mv"] / 1000.0)) * 1000.0,
            "v_reset_mv": float(dyn.get("V_reset", _ALLEN_DEFAULT_GLIF3["v_reset_mv"] / 1000.0)) * 1000.0,
            "v_thresh_mv": float(dyn.get("V_th", _ALLEN_DEFAULT_GLIF3["v_thresh_mv"] / 1000.0)) * 1000.0,
            "refractory_ms": float(dyn.get("t_ref", _ALLEN_DEFAULT_GLIF3["refractory_ms"] / 1000.0)) * 1000.0,
        }

    def info(self) -> dict[str, Any]:
        return {
            "framework": "bmtk",
            "available": self.available,
            "error": self._error,
            "role": "mouse.glif_source",
            "defaults": self.allen_default_glif3(),
        }
