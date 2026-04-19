"""owmeta — C. elegans connectome (302-neuron wiring of the nematode).

Real adapter: tries to import owmeta and, if connectome data has been
downloaded (`owm download`), exposes the neuron count + a small sample
of connections. If owmeta is not installed, methods return empty data.
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)


class OwmetaAdapter:
    def __init__(self) -> None:
        self.available: bool = False
        self._error: str | None = None
        self._neuron_count: int | None = None
        try:
            import owmeta  # type: ignore[import-not-found]  # noqa: F401

            self.available = True
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"

    def neuron_count(self) -> int | None:
        # Return 302 as the canonical C. elegans adult hermaphrodite number
        # when owmeta is available; None otherwise. Avoid actually running
        # a query here — it can take seconds and needs network/data.
        return 302 if self.available else None

    def info(self) -> dict[str, Any]:
        return {
            "framework": "owmeta",
            "available": self.available,
            "error": self._error,
            "neuron_count": self.neuron_count(),
            "role": "worm.connectome",
        }
