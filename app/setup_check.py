"""First-run setup utilities for Project Chimera.

OWNER: Agent-Onboarding.

Provides:
    - marker_path / is_complete / mark_complete : first-run state helpers
    - check_ollama : probe the local Ollama daemon
    - check_model  : verify a specific model is present
    - stream_pull  : async generator that yields normalized pull progress events
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx


# ---------------------------------------------------------------------------
# First-run marker
# ---------------------------------------------------------------------------

def marker_path() -> Path:
    """Resolve the on-disk location of the first-run marker file.

    Windows  -> %APPDATA%/Chimera/setup_complete
    Other    -> ~/.chimera/setup_complete
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Chimera" / "setup_complete"
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".chimera" / "setup_complete"


def is_complete() -> bool:
    """True once setup has been marked complete on this machine."""
    try:
        return marker_path().exists()
    except Exception:
        return False


def mark_complete() -> None:
    """Create the marker file (and parent dir) to remember setup finished."""
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("ok", encoding="utf-8")


# ---------------------------------------------------------------------------
# Ollama probes
# ---------------------------------------------------------------------------

async def check_ollama(host: str) -> dict:
    """Return {'reachable': bool, 'version': str | None, 'url': host}."""
    url = host.rstrip("/")
    out: dict[str, Any] = {"reachable": False, "version": None, "url": host}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url}/api/version")
            if r.status_code == 200:
                out["reachable"] = True
                try:
                    data = r.json()
                    out["version"] = data.get("version")
                except Exception:
                    out["version"] = None
            else:
                # A 404 still means the daemon is up; fall back to root probe.
                try:
                    r2 = await client.get(f"{url}/")
                    out["reachable"] = r2.status_code < 500
                except Exception:
                    pass
    except Exception:
        # Connection refused, timeout, DNS error — daemon is not reachable.
        out["reachable"] = False
    return out


def _model_matches(entry: dict, target: str) -> bool:
    """True if an /api/tags entry refers to the target model.

    Ollama has historically used either `name` or `model` as the field name,
    and tags may be implicit (`qwen2.5:0.5b` vs. `qwen2.5:0.5b-instruct`).
    """
    candidates = [entry.get("name"), entry.get("model")]
    for c in candidates:
        if not c:
            continue
        if c == target:
            return True
        # Match by base name (prefix before ':') if user gave no explicit tag
        if ":" not in target and c.split(":", 1)[0] == target:
            return True
        # Match by base if daemon reports bare name
        if ":" not in c and target.split(":", 1)[0] == c:
            return True
    return False


async def check_model(host: str, model: str) -> dict:
    """Return {'present': bool, 'size_bytes': int | None, 'model': model}."""
    url = host.rstrip("/")
    out: dict[str, Any] = {"present": False, "size_bytes": None, "model": model}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{url}/api/tags")
            if r.status_code != 200:
                return out
            data = r.json()
            for entry in data.get("models", []) or []:
                if _model_matches(entry, model):
                    out["present"] = True
                    size = entry.get("size")
                    if isinstance(size, (int, float)):
                        out["size_bytes"] = int(size)
                    break
    except Exception:
        pass
    return out


async def stream_pull(host: str, model: str) -> AsyncIterator[dict]:
    """Stream `ollama pull` progress as normalized dict events.

    Yields dicts shaped like:
        {"status": "...", "percent": <float|None>, "digest": <str|None>,
         "total": <int|None>, "completed": <int|None>, "done": <bool>}
    The final event has "done": True. Errors yield {"status": "error", ...}.
    """
    url = host.rstrip("/")
    body = {"model": model, "stream": True}
    # No overall timeout — pulls can take minutes.
    timeout = httpx.Timeout(None, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{url}/api/pull", json=body) as resp:
                if resp.status_code != 200:
                    text = ""
                    try:
                        text = (await resp.aread()).decode("utf-8", "replace")
                    except Exception:
                        pass
                    yield {
                        "status": "error",
                        "error": f"HTTP {resp.status_code}: {text[:200]}",
                        "done": True,
                    }
                    return
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    total = ev.get("total")
                    completed = ev.get("completed")
                    percent: Optional[float] = None
                    if isinstance(total, (int, float)) and total and isinstance(completed, (int, float)):
                        percent = round(float(completed) / float(total) * 100.0, 2)
                    status = ev.get("status", "")
                    done = bool(ev.get("done")) or status.lower() == "success"
                    yield {
                        "status": status or ("success" if done else "pulling"),
                        "percent": percent,
                        "digest": ev.get("digest"),
                        "total": int(total) if isinstance(total, (int, float)) else None,
                        "completed": int(completed) if isinstance(completed, (int, float)) else None,
                        "done": done,
                        "error": ev.get("error"),
                    }
                    if done:
                        return
    except httpx.HTTPError as exc:
        yield {"status": "error", "error": str(exc), "done": True}
    except Exception as exc:  # pragma: no cover — belt and suspenders
        yield {"status": "error", "error": str(exc), "done": True}
