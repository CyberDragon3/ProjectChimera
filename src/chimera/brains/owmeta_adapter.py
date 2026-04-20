"""OpenWorm adapter for local connectome bundle inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from chimera.brains.openworm_shards import bundle_summary

log = structlog.get_logger(__name__)


class OwmetaAdapter:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.available: bool = False
        self._error: str | None = None
        self._neuron_count: int | None = None
        self._sample: list[str] = []
        self._graphs_dir: str | None = None

        try:
            summary = bundle_summary(base_dir)
            self._graphs_dir = str(summary["graphs_dir"])
            neuron_count = int(summary["neuron_count"])
            self._sample = list(summary["sample"])
            if summary["present"] and neuron_count > 0:
                self.available = True
                self._neuron_count = neuron_count
            elif not summary["present"]:
                self._error = f"OpenWorm bundle graphs not found at {self._graphs_dir}"
            else:
                self._error = f"No neuron name triples found in {self._graphs_dir}"
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"

        if self.available:
            log.info(
                "brains.owmeta.bundle_detected",
                graphs_dir=self._graphs_dir,
                neuron_count=self._neuron_count,
            )
        else:
            log.debug("brains.owmeta.unavailable", error=self._error, graphs_dir=self._graphs_dir)

    def neuron_count(self) -> int | None:
        return self._neuron_count

    def sample(self) -> list[str]:
        return list(self._sample)

    def info(self) -> dict[str, Any]:
        return {
            "framework": "owmeta",
            "available": self.available,
            "error": self._error,
            "neuron_count": self.neuron_count(),
            "sample": self.sample(),
            "graphs_dir": self._graphs_dir,
            "role": "worm.connectome",
        }
