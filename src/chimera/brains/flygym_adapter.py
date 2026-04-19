"""flygym — MuJoCo-based fruit fly biomechanics (FlyGym + NeuroMechFly).

We don't run the MuJoCo physics inside the reflex loop (too heavy). The
adapter just reports availability so the dashboard can surface 'full
bio-fidelity' status, and returns the default arena/model names so
offline demos can reference them.
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)


class FlygymAdapter:
    def __init__(self) -> None:
        self.available: bool = False
        self._error: str | None = None
        try:
            import flygym  # type: ignore[import-not-found]  # noqa: F401

            self.available = True
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"

    def info(self) -> dict[str, Any]:
        return {
            "framework": "flygym",
            "available": self.available,
            "error": self._error,
            "role": "fly.biomechanics",
            "default_arena": "FlatTerrain",
            "default_model": "NeuroMechFly",
        }
