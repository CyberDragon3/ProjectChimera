"""Dashboard /state endpoint test."""

from __future__ import annotations

from fastapi.testclient import TestClient

from chimera.bus import Bus
from chimera.dashboard.app import create_app
from chimera.store import RingBuffer


def test_state_endpoint_returns_thermal() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    buf.append(55.0)
    app = create_app(bus, buf)
    with TestClient(app) as client:
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        assert j["thermal"]["latest_c"] == 55.0
        assert "recent_events" in j


def test_index_page_served() -> None:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    app = create_app(bus, buf)
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert b"CHIMERA" in r.content
