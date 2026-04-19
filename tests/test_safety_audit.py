"""Static audit: no module may call psutil terminate/kill/suspend outside
``chimera.reflexes.worm``, ``chimera.safety``, or ``chimera.reflexes.lysosome``.

Additionally, every destructive call in ``lysosome`` must be preceded by a
``safety.gate(...)`` call within the same source-level function — **except**
when the destructive call lives inside a ``*Backend`` class. Backend classes
are raw per-handle wrappers around OS primitives; the *caller* (the reflex's
``_sweep``) is responsible for gating the decision to destroy. Requiring each
backend method to re-gate would couple backends to ``ProtectedSpecies``, which
violates the Protocol-first design. See design §5.5 and §7.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "chimera"

DESTRUCTIVE = {"terminate", "kill", "suspend"}
ALLOWED_MODULES = {
    "chimera.reflexes.worm",  # wraps in safety.gate
    "chimera.safety",
    "chimera.reflexes.lysosome",  # Phase 6 — _sweep gates; Backend wraps raw
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(rel.parts)


def test_no_destructive_calls_outside_allowlist() -> None:
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


def _is_safety_gate_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "gate":
        return True
    if isinstance(func, ast.Name) and func.id == "gate":
        return True
    return False


def _is_destructive_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in DESTRUCTIVE
    )


def _collect_backend_function_ids(tree: ast.AST) -> set[int]:
    """Return id() of every FunctionDef/AsyncFunctionDef whose enclosing
    ClassDef name ends with ``Backend``. Those are raw OS-primitive wrappers
    exempt from the per-function gate rule (the caller gates instead)."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Backend"):
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    exempt.add(id(child))
    return exempt


def test_lysosome_kill_calls_are_gated() -> None:
    """Every destructive call in ``lysosome.py`` must sit in a function that
    also contains a ``safety.gate(...)`` call earlier in source order — unless
    the function is a method of a ``*Backend`` class (raw wrapper, caller-gated).
    """
    tree = ast.parse((SRC / "reflexes" / "lysosome.py").read_text(encoding="utf-8"))
    backend_fn_ids = _collect_backend_function_ids(tree)

    ungated: list[tuple[str, int]] = []
    for func in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        if id(func) in backend_fn_ids:
            continue  # raw OS wrapper — caller is responsible for gating
        gate_seen = False
        for node in ast.walk(func):
            if _is_safety_gate_call(node):
                gate_seen = True
            elif _is_destructive_call(node):
                if not gate_seen:
                    ungated.append((func.name, node.lineno))
    assert not ungated, (
        "Ungated destructive calls in lysosome.py:\n"
        + "\n".join(f"  {fn} at line {ln}" for fn, ln in ungated)
    )
