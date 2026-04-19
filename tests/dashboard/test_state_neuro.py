"""Dashboard /state neuro-block tests (Task A8)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from chimera.bus import Bus, Event
from chimera.dashboard.app import create_app
from chimera.store import RingBuffer


def _make_client() -> tuple[Bus, TestClient]:
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    app = create_app(bus, buf)
    client = TestClient(app)
    return bus, client


def test_state_has_neuro_block() -> None:
    bus, client = _make_client()
    with client:
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        assert "neuro" in j
        neuro = j["neuro"]
        assert "dopamine" in neuro
        assert "mouse_rate" in neuro
        assert "last_zebrafish_spike" in neuro
        assert "last_fly_spike" in neuro
        assert neuro["dopamine"]["level"] == 0.0
        assert neuro["dopamine"]["hit_rate"] == 0.5
        assert neuro["dopamine"]["last_outcome"] is None
        assert neuro["dopamine"]["ts"] is None
        assert neuro["mouse_rate"]["e_rate_hz"] == 0.0
        assert neuro["mouse_rate"]["i_rate_hz"] == 0.0
        assert neuro["last_zebrafish_spike"] is None
        assert neuro["last_fly_spike"] is None
    _ = bus


def test_neuro_dopamine_updates() -> None:
    bus, client = _make_client()
    with client:
        bus.publish(
            Event(
                topic="neuro.dopamine",
                payload={"level": 0.42, "hit_rate": 0.8, "last_outcome": "hit"},
                ts=123.0,
            )
        )
        time.sleep(0.1)
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        dopa = j["neuro"]["dopamine"]
        assert dopa["level"] == 0.42
        assert dopa["hit_rate"] == 0.8
        assert dopa["last_outcome"] == "hit"
        assert dopa["ts"] == 123.0


def test_neuro_mouse_rate_updates() -> None:
    bus, client = _make_client()
    with client:
        bus.publish(
            Event(
                topic="neuro.mouse.rate",
                payload={"e_rate_hz": 17.5, "i_rate_hz": 4.2},
                ts=456.0,
            )
        )
        time.sleep(0.1)
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        rate = j["neuro"]["mouse_rate"]
        assert rate["e_rate_hz"] == 17.5
        assert rate["i_rate_hz"] == 4.2
        assert rate["ts"] == 456.0


def test_neuro_zebrafish_spike_captured() -> None:
    bus, client = _make_client()
    with client:
        bus.publish(
            Event(
                topic="neuro.zebrafish.spike",
                payload={"v": -49.5, "current": 12.3},
                ts=789.0,
            )
        )
        time.sleep(0.1)
        r = client.get("/state")
        assert r.status_code == 200
        j = r.json()
        spike = j["neuro"]["last_zebrafish_spike"]
        assert spike is not None
        assert spike["v"] == -49.5
        assert spike["current"] == 12.3
        assert spike["ts"] == 789.0
