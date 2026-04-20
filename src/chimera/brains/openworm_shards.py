"""Helpers for reading OpenWorm graph shards without owmeta_core."""

from __future__ import annotations

from pathlib import Path
import re


_ENCODED_BUNDLE_SEGMENTS = ("openworm%2Fc_elegans", "2", "graphs")
_PLAIN_BUNDLE_SEGMENTS = ("openworm", "c_elegans", "2", "graphs")
_NEURON_NAME_LINE = re.compile(
    r'^<http://data\.openworm\.org/sci/bio/Neuron#(?P<slug>[^>]+)> '
    r'<http://schema\.openworm\.org/2020/07/sci/bio/Cell/name> '
    r'"(?P<name>[^"]+)" \.$'
)


def default_graphs_dir(base_dir: Path | None = None) -> Path:
    """Locate the OpenWorm graph-shard directory from a repo or project root."""
    root = base_dir or Path.cwd()
    bundles_root = root / ".owm" / "bundles"
    encoded = bundles_root.joinpath(*_ENCODED_BUNDLE_SEGMENTS)
    if encoded.is_dir():
        return encoded
    return bundles_root.joinpath(*_PLAIN_BUNDLE_SEGMENTS)


def neuron_names_from_graphs(graphs_dir: Path) -> list[str]:
    """Read distinct neuron names from raw `.nt` bundle shards."""
    if not graphs_dir.is_dir():
        return []

    names: set[str] = set()
    for graph_file in sorted(graphs_dir.glob("*.nt")):
        with graph_file.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                match = _NEURON_NAME_LINE.match(line)
                if match:
                    names.add(match.group("name"))
    return sorted(names)


def bundle_summary(base_dir: Path | None = None) -> dict[str, object]:
    """Return a compact summary of the local OpenWorm shard bundle."""
    graphs_dir = default_graphs_dir(base_dir)
    names = neuron_names_from_graphs(graphs_dir)
    return {
        "graphs_dir": graphs_dir,
        "present": graphs_dir.is_dir(),
        "neuron_names": names,
        "neuron_count": len(names),
        "sample": names[:10],
    }
