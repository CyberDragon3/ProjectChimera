"""Dashboard extras: protected_species, brains_available, runtime_flags, control endpoints."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from chimera.bus import Bus
from chimera.dashboard.app import create_app
from chimera.store import RingBuffer


def _client(**kwargs) -> tuple[Bus, TestClient]:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    app = create_app(bus, buf, **kwargs)
    return bus, TestClient(app)


def test_state_has_protected_species_list() -> None:
    bus, client = _client(protected_species=frozenset({"foo.exe", "bar.exe"}))
    with client:
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        assert "protected_species" in j
        assert "foo.exe" in j["protected_species"]
        assert "bar.exe" in j["protected_species"]
    _ = bus


def test_state_has_brains_available() -> None:
    bus, client = _client(brains_available={"psutil": True, "owmeta": False})
    with client:
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        assert j["brains_available"] == {"psutil": True, "owmeta": False}
    _ = bus


def test_state_has_runtime_flags() -> None:
    bus, client = _client(runtime_flags={"neuro_enabled": True, "lysosome_enabled": False})
    with client:
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        assert j["runtime_flags"]["neuro_enabled"] is True
        assert j["runtime_flags"]["lysosome_enabled"] is False
    _ = bus


def test_state_accepts_initial_worm_state() -> None:
    bus, client = _client(
        worm_state={
            "available": True,
            "neuron_count": 302,
            "active_count": 0,
            "active_fraction": 0.0,
            "status": "idle",
            "sample": ["ADAL", "ADAR"],
            "active_neurons": [],
            "graphs_dir": "C:/graphs",
        }
    )
    with client:
        j = client.get("/state").json()
        assert j["neuro"]["worm"]["available"] is True
        assert j["neuro"]["worm"]["neuron_count"] == 302
        assert j["neuro"]["worm"]["sample"] == ["ADAL", "ADAR"]
    _ = bus


def test_state_defaults_empty_when_no_extras() -> None:
    bus, client = _client()
    with client:
        j = client.get("/state").json()
        assert j["protected_species"] == []
        assert j["brains_available"] == {}
        assert j["runtime_flags"] == {}
    _ = bus


def test_control_toggle_records() -> None:
    bus, client = _client(runtime_flags={"neuro_enabled": True})
    with client:
        r = client.post("/control/toggle/neuro", json={"enabled": False})
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["applied"] is False
        assert "restart" in body["note"].lower()
        j = client.get("/state").json()
        assert j["runtime_flags"]["neuro_enabled"] is False
    _ = bus


def test_control_toggle_unknown_system_rejected() -> None:
    bus, client = _client()
    with client:
        r = client.post("/control/toggle/banana", json={"enabled": True})
        assert r.status_code == 400
    _ = bus


@pytest.mark.asyncio
async def test_control_lysosome_publishes() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    app = create_app(bus, buf)
    # Subscribe BEFORE the request is issued.
    q = bus.subscribe("lysosome.force")
    try:
        with TestClient(app) as client:
            r = client.post("/control/lysosome/trigger")
            assert r.status_code == 200
            assert r.json() == {"ok": True}
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            assert ev.topic == "lysosome.force"
    finally:
        bus.unsubscribe("lysosome.force", q)


@pytest.mark.asyncio
async def test_control_dopamine_reset_publishes() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    app = create_app(bus, buf)
    q = bus.subscribe("neuro.dopamine.reset")
    try:
        with TestClient(app) as client:
            r = client.post("/control/dopamine/reset")
            assert r.status_code == 200
            assert r.json() == {"ok": True}
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            assert ev.topic == "neuro.dopamine.reset"
    finally:
        bus.unsubscribe("neuro.dopamine.reset", q)


def test_existing_state_endpoints_still_work() -> None:
    """Regression: existing callers without extras kwargs keep working."""
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    app = create_app(bus, buf)
    with TestClient(app) as client:
        j = client.get("/state").json()
        assert "neuro" in j
        assert "thermal" in j
        assert "recent_events" in j
    _ = time  # silence unused import on platforms where the symbol is optional
