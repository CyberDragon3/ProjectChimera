"""Tests for Agent-Dashboard FastAPI server."""
from __future__ import annotations

import asyncio
import io
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.dashboard.server import build_app


# ---------------------------------------------------------------------------
# Minimal fakes (self-contained — do not import tier implementations)
# ---------------------------------------------------------------------------

@dataclass
class _FlyPol:
    sensitivity: float = 0.5
    looming_threshold: float = 0.35


@dataclass
class _WormPol:
    cpu_pain_threshold: float = 0.85
    ram_pain_threshold: float = 0.90
    poke_derivative: float = 0.25
    dwell_ms: int = 800


@dataclass
class _MousePol:
    track_target_xy: Optional[tuple] = None
    error_threshold: float = 120.0
    consecutive_frames: int = 3


@dataclass
class _BioPolicy:
    fly: _FlyPol = field(default_factory=_FlyPol)
    worm: _WormPol = field(default_factory=_WormPol)
    mouse: _MousePol = field(default_factory=_MousePol)

    def to_dict(self) -> dict:
        return {
            "fly": {
                "sensitivity": self.fly.sensitivity,
                "looming_threshold": self.fly.looming_threshold,
            },
            "worm": {
                "cpu_pain_threshold": self.worm.cpu_pain_threshold,
                "ram_pain_threshold": self.worm.ram_pain_threshold,
                "poke_derivative": self.worm.poke_derivative,
                "dwell_ms": self.worm.dwell_ms,
            },
            "mouse": {
                "track_target_xy": list(self.mouse.track_target_xy) if self.mouse.track_target_xy else None,
                "error_threshold": self.mouse.error_threshold,
                "consecutive_frames": self.mouse.consecutive_frames,
            },
        }


@dataclass
class _InterruptEvent:
    module: str
    kind: str
    payload: dict = field(default_factory=dict)
    t_stimulus_ns: int = 0
    t_fire_ns: int = 0
    t_action_ns: int = 0

    def latency_us(self) -> float:
        if self.t_action_ns and self.t_stimulus_ns:
            return (self.t_action_ns - self.t_stimulus_ns) / 1_000.0
        return 0.0


@dataclass
class _ExecutiveEvent:
    t_ns: int = 0
    kind: str = "status"
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class _Pressure:
    t_ns: int = 0
    cpu: float = 0.3
    ram: float = 0.4
    pressure: float = 0.35
    derivative: float = 0.02


@dataclass
class _Cursor:
    t_ns: int = 0
    x: int = 100
    y: int = 200
    vx: float = 0.0
    vy: float = 0.0


@dataclass
class _OmmatidiaFrame:
    t_ns: int
    luminance: np.ndarray
    diff: np.ndarray


@dataclass
class _Snapshot:
    policy: Optional[_BioPolicy] = None
    ommatidia: Optional[_OmmatidiaFrame] = None
    pressure: Optional[_Pressure] = None
    cursor: Optional[_Cursor] = None
    sugar_concentration: float = 0.0
    fly_spikes: deque = field(default_factory=lambda: deque(maxlen=300))
    worm_spikes: deque = field(default_factory=lambda: deque(maxlen=300))
    mouse_spikes: deque = field(default_factory=lambda: deque(maxlen=300))
    recent_interrupts: deque = field(default_factory=lambda: deque(maxlen=32))
    recent_executive: deque = field(default_factory=lambda: deque(maxlen=32))


class _PolicyStore:
    def __init__(self, policy: _BioPolicy) -> None:
        self._p = policy
        self._version = 0

    def get(self) -> _BioPolicy:
        return self._p

    @property
    def version(self) -> int:
        return self._version

    def set(self, p: _BioPolicy) -> None:
        self._p = p
        self._version += 1


class _FanoutBus:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []

    def subscribe(self, maxsize: int = 64) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    async def publish(self, ev: Any) -> None:
        for q in self._subs:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await q.put(ev)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return {
        "server": {"host": "127.0.0.1", "port": 8000, "ws_hz": 30},
    }


@pytest.fixture
def fakes():
    snap = _Snapshot()
    snap.policy = _BioPolicy()
    snap.pressure = _Pressure(t_ns=time.perf_counter_ns())
    snap.cursor = _Cursor(t_ns=time.perf_counter_ns())
    snap.sugar_concentration = 0.5
    snap.ommatidia = _OmmatidiaFrame(
        t_ns=time.perf_counter_ns(),
        luminance=np.random.rand(32, 32).astype(np.float32),
        diff=(np.random.rand(32, 32).astype(np.float32) - 0.5),
    )
    snap.fly_spikes.extend([time.perf_counter_ns() - i * 1_000_000 for i in range(10)])
    snap.worm_spikes.extend([time.perf_counter_ns() - i * 2_000_000 for i in range(5)])
    snap.mouse_spikes.extend([time.perf_counter_ns() - i * 3_000_000 for i in range(3)])
    snap.recent_executive.append(
        _ExecutiveEvent(t_ns=time.perf_counter_ns(), kind="explain", text="Jarvis confirmed the policy change.")
    )

    ps = _PolicyStore(_BioPolicy())
    exec_bus = _FanoutBus()
    intr_bus = _FanoutBus()
    cmd_queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    return {
        "snapshot": snap,
        "policy_store": ps,
        "exec_bus": exec_bus,
        "interrupt_bus": intr_bus,
        "command_queue": cmd_queue,
    }


@pytest.fixture
def app(fakes, cfg, monkeypatch, tmp_path):
    # Make sure index.html exists for test 1; create a minimal placeholder if needed.
    from pathlib import Path
    from app import setup_check
    static = Path(__file__).resolve().parents[1] / "dashboard" / "static"
    idx = static / "index.html"
    if not idx.exists():
        idx.write_text(
            "<!doctype html><html><body><h1>CHIMERA command bar</h1></body></html>",
            encoding="utf-8",
        )
    marker = tmp_path / "chimera" / "setup_complete"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(setup_check, "marker_path", lambda: marker)
    return build_app(
        snapshot=fakes["snapshot"],
        policy_store=fakes["policy_store"],
        exec_bus=fakes["exec_bus"],
        interrupt_bus=fakes["interrupt_bus"],
        command_queue=fakes["command_queue"],
        cfg=cfg,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_root_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text.lower()
    assert ("jarvis" in body) or ("chimera" in body) or ("command" in body)


def test_dashboard_ok(client):
    r = client.get("/dashboard")
    assert r.status_code == 200


def test_post_command(client, fakes):
    r = client.post("/api/command", json={"text": "hi"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # Drain queue
    assert not fakes["command_queue"].empty()
    got = fakes["command_queue"].get_nowait()
    assert got == "hi"


def test_post_voice(client):
    files = {"file": ("sample.wav", io.BytesIO(b"RIFF....WAVE"), "audio/wav")}
    r = client.post("/api/voice", files=files)
    assert r.status_code == 200
    body = r.json()
    assert "text" in body
    assert isinstance(body["text"], str)


def test_get_policy(client):
    r = client.get("/api/policy")
    assert r.status_code == 200
    body = r.json()
    assert set(["fly", "worm", "mouse"]).issubset(body.keys())


def test_ws_snapshot_frame(client):
    with client.websocket_connect("/ws") as ws:
        deadline = time.time() + 0.5
        saw_snapshot = False
        while time.time() < deadline:
            data = ws.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            if msg.get("event") == "snapshot":
                saw_snapshot = True
                assert "data" in msg
                assert "recent_executive" in msg["data"]
                break
        assert saw_snapshot, "did not receive snapshot frame within 500 ms"


def test_ws_reflex_frame(client, fakes):
    with client.websocket_connect("/ws") as ws:
        # Publish an interrupt onto the interrupt_bus
        # TestClient runs on a portal loop; use its portal.
        ev = _InterruptEvent(
            module="worm",
            kind="ava_recoil",
            t_stimulus_ns=time.perf_counter_ns() - 5_000,
            t_fire_ns=time.perf_counter_ns() - 4_000,
            t_action_ns=time.perf_counter_ns(),
        )
        _publish_from_sync(client, fakes["interrupt_bus"], ev)

        deadline = time.time() + 1.0
        saw = False
        while time.time() < deadline:
            data = ws.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            if msg.get("event") == "reflex":
                saw = True
                assert msg["data"]["module"] == "worm"
                assert msg["data"]["kind"] == "ava_recoil"
                assert "latency_us" in msg["data"]
                break
        assert saw, "did not receive reflex frame within time"


def test_ws_executive_frame(client, fakes):
    with client.websocket_connect("/ws") as ws:
        ev = _ExecutiveEvent(t_ns=time.perf_counter_ns(), kind="status", text="hello world")
        _publish_from_sync(client, fakes["exec_bus"], ev)

        deadline = time.time() + 1.0
        saw = False
        while time.time() < deadline:
            data = ws.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            if msg.get("event") == "executive":
                saw = True
                assert msg["data"]["text"] == "hello world"
                break
        assert saw, "did not receive executive frame within time"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _publish_from_sync(client: TestClient, bus: _FanoutBus, ev: Any) -> None:
    """Publish an event onto the bus from a sync test, using TestClient's portal."""
    portal = getattr(client, "portal", None)
    if portal is not None:
        portal.call(bus.publish, ev)
        return
    # Fallback: run in a fresh loop and post into the first sub's queue directly.
    # Since TestClient uses its own event loop, we just call-soon-threadsafe via
    # asyncio if reachable; but in practice TestClient exposes .portal.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(bus.publish(ev))
    finally:
        loop.close()
