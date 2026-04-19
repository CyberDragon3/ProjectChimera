"""Negative-space safety audit for ``chimera.neuro.*``.

The neuro layer is pure computation — LIF/GLIF integrators, dopamine scalars,
and the like. It must never touch destructive OS primitives and it must never
need the safety gate (because it never proposes actions against real processes).

Two assertions:

1. No ``.terminate()`` / ``.kill()`` / ``.suspend()`` call anywhere under
   ``src/chimera/neuro/``.
2. No import of ``chimera.safety`` (belt-and-braces — if neuro tries to reach
   for the gate, the architecture is already wrong).
"""

from __future__ import annotations

import ast
from pathlib import Path

NEURO_DIR = Path(__file__).resolve().parents[2] / "src" / "chimera" / "neuro"
FORBIDDEN = {"terminate", "kill", "suspend"}


def _iter_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def test_neuro_package_has_no_destructive_calls() -> None:
    offenders: list[str] = []
    for py in NEURO_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for call in _iter_calls(tree):
            if call.func.attr in FORBIDDEN:
                offenders.append(f"{py.name}:{call.lineno} .{call.func.attr}()")
    assert not offenders, (
        f"Neuro package must not call destructive methods: {offenders}"
    )


def test_neuro_package_does_not_import_safety() -> None:
    offenders: list[str] = []
    for py in NEURO_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "chimera.safety"
            ):
                offenders.append(f"{py.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("chimera.safety"):
                        offenders.append(f"{py.name}:{node.lineno}")
    assert not offenders, (
        f"Neuro package must not import chimera.safety: {offenders}"
    )
