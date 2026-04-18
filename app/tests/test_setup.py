"""Tests for Agent-Onboarding first-run wizard + backend probes."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import setup_check
from app.dashboard.server import build_app


# ---------------------------------------------------------------------------
# Helpers to build a FastAPI app with no-op fakes
# ---------------------------------------------------------------------------

class _PolicyStore:
    def __init__(self) -> None:
        self._version = 0

    def get(self) -> dict:
        return {"fly": {}, "worm": {}, "mouse": {}}

    @property
    def version(self) -> int:
        return self._version


class _Bus:
    def subscribe(self, maxsize: int = 64) -> asyncio.Queue:
        return asyncio.Queue(maxsize=maxsize)

    async def publish(self, ev: Any) -> None:
        pass


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker_exists: bool = False):
    # Redirect the marker to a tmp-path file we control.
    target = tmp_path / "chimera" / "setup_complete"
    monkeypatch.setattr(setup_check, "marker_path", lambda: target)

    if marker_exists:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ok", encoding="utf-8")

    # Ensure index.html exists (post-setup path).
    static = Path(__file__).resolve().parents[1] / "dashboard" / "static"
    (static / "index.html").exists() or (static / "index.html").write_text(
        "<!doctype html><html><body><h1>CHIMERA</h1></body></html>", encoding="utf-8"
    )

    cfg = {
        "server": {"host": "127.0.0.1", "port": 8000, "ws_hz": 30},
        "ollama": {"host": "http://localhost:11434", "model": "qwen2.5:0.5b"},
    }
    app = build_app(
        snapshot=type("S", (), {"pressure": None, "cursor": None, "ommatidia": None,
                                "sugar_concentration": 0.0, "fly_spikes": [],
                                "worm_spikes": [], "mouse_spikes": [],
                                "recent_interrupts": []})(),
        policy_store=_PolicyStore(),
        exec_bus=_Bus(),
        interrupt_bus=_Bus(),
        command_queue=asyncio.Queue(maxsize=8),
        cfg=cfg,
    )
    return app, target


# ---------------------------------------------------------------------------
# Fake httpx async client
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None, raise_exc: Exception | None = None):
        self.status_code = status_code
        self._json = json_data
        self._raise = raise_exc

    def json(self) -> Any:
        return self._json


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient covering get()."""

    def __init__(self, *, get_map: dict | None = None, get_exc: Exception | None = None, **_kwargs):
        self._get_map = get_map or {}
        self._get_exc = get_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        if self._get_exc is not None:
            raise self._get_exc
        for suffix, resp in self._get_map.items():
            if url.endswith(suffix):
                return resp
        return _FakeResponse(status_code=404, json_data={})


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, *, get_map=None, get_exc=None):
    import httpx

    def factory(*args, **kwargs):
        return _FakeAsyncClient(get_map=get_map, get_exc=get_exc)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# 1. marker_path honors env, is_complete() false by default
# ---------------------------------------------------------------------------

def test_marker_path_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if sys.platform.startswith("win"):
        monkeypatch.setenv("APPDATA", str(tmp_path))
        p = setup_check.marker_path()
        assert str(p).startswith(str(tmp_path))
        assert p.name == "setup_complete"
        assert p.parent.name == "Chimera"
    else:
        monkeypatch.setenv("HOME", str(tmp_path))
        p = setup_check.marker_path()
        assert str(p).startswith(str(tmp_path))
        assert p.name == "setup_complete"
        assert p.parent.name == ".chimera"

    # Redirect to a tmp path so is_complete() is observably False
    target = tmp_path / "xx" / "setup_complete"
    monkeypatch.setattr(setup_check, "marker_path", lambda: target)
    assert setup_check.is_complete() is False


# ---------------------------------------------------------------------------
# 2. mark_complete creates file + dir; is_complete() flips to True
# ---------------------------------------------------------------------------

def test_mark_complete_creates_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "chimera" / "setup_complete"
    monkeypatch.setattr(setup_check, "marker_path", lambda: target)
    assert not target.exists()
    setup_check.mark_complete()
    assert target.exists()
    assert setup_check.is_complete() is True
    # cleanup
    target.unlink()
    assert setup_check.is_complete() is False


# ---------------------------------------------------------------------------
# 3. GET / redirects to /setup when marker missing
# ---------------------------------------------------------------------------

def test_root_redirects_when_marker_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app, _target = _make_app(tmp_path, monkeypatch, marker_exists=False)
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/setup"


# ---------------------------------------------------------------------------
# 4. GET / serves HTML when marker present
# ---------------------------------------------------------------------------

def test_root_serves_when_marker_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app, target = _make_app(tmp_path, monkeypatch, marker_exists=True)
    try:
        with TestClient(app) as client:
            r = client.get("/", follow_redirects=False)
            assert r.status_code == 200
            ctype = r.headers.get("content-type", "")
            assert "html" in ctype.lower()
            assert "/static/css/tokens.css" in r.text
            assert "/static/js/bar.js" in r.text
    finally:
        if target.exists():
            target.unlink()

def test_dashboard_redirects_when_marker_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app, _target = _make_app(tmp_path, monkeypatch, marker_exists=False)
    with TestClient(app) as client:
        r = client.get("/dashboard", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/setup"

def test_setup_page_uses_static_assets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app, _target = _make_app(tmp_path, monkeypatch, marker_exists=False)
    with TestClient(app) as client:
        r = client.get("/setup", follow_redirects=False)
        assert r.status_code == 200
        assert "/static/css/tokens.css" in r.text
        assert "/static/css/setup.css" in r.text
        assert "/static/js/setup.js" in r.text


# ---------------------------------------------------------------------------
# 5. /api/setup/status returns expected keys (httpx mocked)
# ---------------------------------------------------------------------------

def test_status_endpoint_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app, _target = _make_app(tmp_path, monkeypatch, marker_exists=False)
    _patch_httpx(
        monkeypatch,
        get_map={
            "/api/version": _FakeResponse(200, {"version": "0.3.0"}),
            "/api/tags": _FakeResponse(200, {"models": [{"name": "qwen2.5:0.5b", "size": 400_000_000}]}),
        },
    )
    with TestClient(app) as client:
        r = client.get("/api/setup/status")
        assert r.status_code == 200
        body = r.json()
        assert set(["marker", "ollama", "model"]).issubset(body.keys())
        assert body["ollama"]["reachable"] is True
        assert body["model"]["present"] is True
        assert body["marker"] is False


# ---------------------------------------------------------------------------
# 6. check_ollama: reachable=False on connection error (no raise)
# ---------------------------------------------------------------------------

def test_check_ollama_handles_connection_error(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx(monkeypatch, get_exc=ConnectionRefusedError("nope"))
    out = asyncio.run(setup_check.check_ollama("http://localhost:11434"))
    assert out["reachable"] is False
    assert out["version"] is None
    assert out["url"] == "http://localhost:11434"


# ---------------------------------------------------------------------------
# 7. check_model: name vs model field variance, and missing case
# ---------------------------------------------------------------------------

def test_check_model_present_and_absent(monkeypatch: pytest.MonkeyPatch):
    # Case A: uses "model" field (not "name"). Should still be detected.
    _patch_httpx(
        monkeypatch,
        get_map={
            "/api/tags": _FakeResponse(200, {"models": [{"model": "qwen2.5:0.5b", "size": 123}]}),
        },
    )
    out = asyncio.run(setup_check.check_model("http://localhost:11434", "qwen2.5:0.5b"))
    assert out["present"] is True
    assert out["size_bytes"] == 123
    assert out["model"] == "qwen2.5:0.5b"

    # Case B: target absent.
    _patch_httpx(
        monkeypatch,
        get_map={
            "/api/tags": _FakeResponse(200, {"models": [{"name": "llama3:8b"}]}),
        },
    )
    out = asyncio.run(setup_check.check_model("http://localhost:11434", "qwen2.5:0.5b"))
    assert out["present"] is False


# ---------------------------------------------------------------------------
# 8. POST /api/setup/mark_complete creates marker + returns payload
# ---------------------------------------------------------------------------

def test_mark_complete_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app, target = _make_app(tmp_path, monkeypatch, marker_exists=False)
    try:
        with TestClient(app) as client:
            assert not target.exists()
            r = client.post("/api/setup/mark_complete")
            assert r.status_code == 200
            assert r.json() == {"ok": True, "redirect": "/"}
            assert target.exists()
    finally:
        if target.exists():
            target.unlink()
