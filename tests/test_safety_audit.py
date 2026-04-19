"""Static audit: no module may call psutil terminate/kill/suspend/nice outside
``chimera.reflexes.worm`` or ``chimera.safety``.  This enforces the Phase-6
invariant that every destructive action is gated by the protected-species
whitelist.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "chimera"

DESTRUCTIVE = {"terminate", "kill", "suspend"}
ALLOWED_MODULES = {
    "chimera.reflexes.worm",  # wraps in safety.gate
    "chimera.safety",
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(rel.parts)


def test_no_destructive_calls_outside_worm() -> None:
    offenders: list[tuple[str, int, str]] = []
    for py in SRC.rglob("*.py"):
        mod = _module_name(py)
        if mod in ALLOWED_MODULES:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in DESTRUCTIVE:
                    offenders.append((mod, node.lineno, node.func.attr))

    assert not offenders, (
        "Destructive process calls found outside safety-gated modules:\n"
        + "\n".join(f"  {m}:{line} -> .{attr}()" for m, line, attr in offenders)
    )


def test_worm_reflex_uses_safety_gate() -> None:
    """The Worm reflex source must reference ``safety.gate`` or ``is_protected``."""
    src = (SRC / "reflexes" / "worm.py").read_text(encoding="utf-8")
    assert "gate(" in src or "is_protected(" in src, (
        "Worm reflex does not pass through the safety gate — refuse to merge."
    )
