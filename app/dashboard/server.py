"""FastAPI dashboard + command bar server.

OWNER: Agent-Dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import is_dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import setup_check

log = logging.getLogger("chimera.dashboard")

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _jsonify(obj: Any) -> Any:
    """Recursively convert numpy / dataclass / deque objects to JSON-safe types."""
    if obj is None:
        return None
    if isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, np.ndarray):
        return obj.round(3).tolist()
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if hasattr(obj, "__iter__") and not isinstance(obj, (bytes, bytearray)):
        try:
            return [_jsonify(v) for v in list(obj)]
        except TypeError:
            pass
    if is_dataclass(obj):
        return _jsonify(asdict(obj))
    return str(obj)


def _downsample_16(arr: np.ndarray) -> np.ndarray:
    """Block-reduce a 2D array to 16x16 using mean pooling."""
    if arr is None:
        return None
    a = np.asarray(arr, dtype=np.float32)
    if a.ndim != 2:
        return a
    h, w = a.shape
    if h == 16 and w == 16:
        return a
    # Pad to a shape divisible by 16
    ph = (16 - h % 16) % 16
    pw = (16 - w % 16) % 16
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw)), mode="edge")
    h2, w2 = a.shape
    bh, bw = h2 // 16, w2 // 16
    if bh < 1 or bw < 1:
        # Upsample via repeat
        ry = max(1, 16 // max(h, 1))
        rx = max(1, 16 // max(w, 1))
        a2 = np.kron(a, np.ones((ry, rx), dtype=np.float32))
        return a2[:16, :16]
    a = a[: bh * 16, : bw * 16]
    return a.reshape(16, bh, 16, bw).mean(axis=(1, 3))


def _serialize_interrupt(ev: Any) -> dict[str, Any]:
    d = asdict(ev) if is_dataclass(ev) else dict(getattr(ev, "__dict__", {}))
    d = _jsonify(d)
    try:
        d["latency_us"] = ev.latency_us()
    except Exception:
        d["latency_us"] = 0.0
    return d


def _serialize_executive(ev: Any) -> dict[str, Any]:
    return _jsonify(asdict(ev) if is_dataclass(ev) else dict(getattr(ev, "__dict__", {})))


def _serialize_snapshot(snapshot: Any, policy_store: Any) -> dict[str, Any]:
    policy = None
    if policy_store is not None:
        try:
            p = policy_store.get()
            if hasattr(p, "to_dict"):
                policy = p.to_dict()
            else:
                policy = _jsonify(p)
        except Exception:
            policy = None

    pressure = None
    if getattr(snapshot, "pressure", None) is not None:
        p = snapshot.pressure
        pressure = {
            "t_ns": int(getattr(p, "t_ns", 0)),
            "cpu": float(getattr(p, "cpu", 0.0)),
            "ram": float(getattr(p, "ram", 0.0)),
            "pressure": float(getattr(p, "pressure", 0.0)),
            "derivative": float(getattr(p, "derivative", 0.0)),
        }

    cursor = None
    if getattr(snapshot, "cursor", None) is not None:
        c = snapshot.cursor
        cursor = {
            "t_ns": int(getattr(c, "t_ns", 0)),
            "x": int(getattr(c, "x", 0)),
            "y": int(getattr(c, "y", 0)),
            "vx": float(getattr(c, "vx", 0.0)),
            "vy": float(getattr(c, "vy", 0.0)),
        }

    omm = getattr(snapshot, "ommatidia", None)
    omm_payload: Optional[dict[str, Any]] = None
    if omm is not None:
        lum = getattr(omm, "luminance", None)
        diff = getattr(omm, "diff", None)
        lum16 = _downsample_16(lum) if lum is not None else None
        diff16 = _downsample_16(diff) if diff is not None else None
        omm_payload = {
            "t_ns": int(getattr(omm, "t_ns", 0)),
            "luminance": lum16.round(3).tolist() if lum16 is not None else None,
            "diff": diff16.round(3).tolist() if diff16 is not None else None,
        }

    fly_spikes = list(getattr(snapshot, "fly_spikes", []) or [])
    worm_spikes = list(getattr(snapshot, "worm_spikes", []) or [])
    mouse_spikes = list(getattr(snapshot, "mouse_spikes", []) or [])

    recent_interrupts = []
    for ev in list(getattr(snapshot, "recent_interrupts", []) or []):
        recent_interrupts.append(_serialize_interrupt(ev))

    return {
        "t_ns": time.perf_counter_ns(),
        "policy": policy,
        "pressure": pressure,
        "cursor": cursor,
        "sugar_concentration": float(getattr(snapshot, "sugar_concentration", 0.0) or 0.0),
        "ommatidia": omm_payload,
        "spikes": {
            "fly": fly_spikes,
            "worm": worm_spikes,
            "mouse": mouse_spikes,
        },
        "recent_interrupts": recent_interrupts,
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(snapshot, policy_store, exec_bus, interrupt_bus, command_queue, cfg) -> FastAPI:
    app = FastAPI(title="Project Chimera Dashboard")

    # Resolve config access regardless of dict/attr style
    def _cfg_get(path: list[str], default=None):
        cur = cfg
        for p in path:
            if cur is None:
                return default
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                cur = getattr(cur, p, None)
        return default if cur is None else cur

    ws_hz = float(_cfg_get(["server", "ws_hz"], 15) or 15)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def root() -> Any:
        # First-run: send the user to the onboarding wizard.
        if not setup_check.is_complete():
            return RedirectResponse(url="/setup", status_code=302)
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        # CommandBar agent hasn't written index.html yet — serve a minimal
        # placeholder that still satisfies "GET / returns HTML".
        return HTMLResponse(
            "<!doctype html><html><head><title>CHIMERA command</title></head>"
            "<body><h1>CHIMERA</h1><p>command bar not yet deployed</p>"
            "<p><a href=\"/dashboard\">dashboard</a></p></body></html>"
        )

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page() -> Any:
        path = STATIC_DIR / "setup.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="setup.html not found")
        return FileResponse(str(path))

    @app.get("/api/setup/status")
    async def setup_status() -> dict:
        host = str(_cfg_get(["ollama", "host"], "http://localhost:11434"))
        model = str(_cfg_get(["ollama", "model"], "qwen2.5:0.5b"))
        ollama = await setup_check.check_ollama(host)
        model_info = {"present": False, "size_bytes": None, "model": model}
        if ollama.get("reachable"):
            model_info = await setup_check.check_model(host, model)
        return {
            "marker": setup_check.is_complete(),
            "ollama": ollama,
            "model": model_info,
        }

    @app.post("/api/setup/pull_model")
    async def setup_pull_model() -> StreamingResponse:
        host = str(_cfg_get(["ollama", "host"], "http://localhost:11434"))
        model = str(_cfg_get(["ollama", "model"], "qwen2.5:0.5b"))

        async def gen():
            async for ev in setup_check.stream_pull(host, model):
                yield (json.dumps(ev) + "\n").encode("utf-8")

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.post("/api/setup/mark_complete")
    async def setup_mark_complete() -> dict:
        setup_check.mark_complete()
        return {"ok": True, "redirect": "/"}

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> Any:
        if not setup_check.is_complete():
            return RedirectResponse(url="/setup", status_code=302)
        path = STATIC_DIR / "dashboard.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="dashboard.html not found")
        return FileResponse(str(path))

    @app.post("/api/command")
    async def post_command(body: dict) -> dict:
        text = (body or {}).get("text", "")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="text must be a string")
        try:
            command_queue.put_nowait(text)
        except asyncio.QueueFull:
            raise HTTPException(status_code=503, detail="command queue full")
        except Exception:
            # Fall back to blocking put for plain queues
            await command_queue.put(text)
        return {"ok": True}

    @app.post("/api/voice")
    async def post_voice(file: UploadFile = File(...)) -> dict:
        _ = await file.read()
        log.warning("voice endpoint stubbed — no transcription wired")
        return {"text": "[voice transcription not wired]"}

    @app.get("/api/policy")
    async def get_policy() -> dict:
        if policy_store is None:
            return {}
        p = policy_store.get()
        if hasattr(p, "to_dict"):
            return p.to_dict()
        return _jsonify(p)

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()

        exec_q = exec_bus.subscribe() if exec_bus is not None else None
        intr_q = interrupt_bus.subscribe() if interrupt_bus is not None else None

        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []

        last_policy_version = getattr(policy_store, "version", 0) if policy_store is not None else 0

        async def _send(frame: dict) -> None:
            try:
                await socket.send_text(json.dumps(_jsonify(frame), default=str))
            except Exception:
                stop.set()
                raise

        async def snapshot_loop() -> None:
            nonlocal last_policy_version
            period = 1.0 / max(ws_hz, 0.5)
            try:
                while not stop.is_set():
                    frame = {
                        "event": "snapshot",
                        "data": _serialize_snapshot(snapshot, policy_store),
                    }
                    await _send(frame)
                    # Policy-change broadcast
                    if policy_store is not None:
                        v = getattr(policy_store, "version", 0)
                        if v != last_policy_version:
                            last_policy_version = v
                            pol = policy_store.get()
                            pol_dict = pol.to_dict() if hasattr(pol, "to_dict") else _jsonify(pol)
                            await _send({"event": "policy", "data": pol_dict})
                    await asyncio.sleep(period)
            except (WebSocketDisconnect, RuntimeError):
                stop.set()
            except Exception as e:
                log.exception("snapshot_loop error: %s", e)
                stop.set()

        async def exec_loop() -> None:
            if exec_q is None:
                return
            try:
                while not stop.is_set():
                    ev = await exec_q.get()
                    await _send({"event": "executive", "data": _serialize_executive(ev)})
            except (WebSocketDisconnect, RuntimeError):
                stop.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("exec_loop error: %s", e)
                stop.set()

        async def intr_loop() -> None:
            if intr_q is None:
                return
            try:
                while not stop.is_set():
                    ev = await intr_q.get()
                    await _send({"event": "reflex", "data": _serialize_interrupt(ev)})
            except (WebSocketDisconnect, RuntimeError):
                stop.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("intr_loop error: %s", e)
                stop.set()

        async def recv_loop() -> None:
            # Drain inbound messages (we don't require any) so disconnect is seen.
            try:
                while not stop.is_set():
                    await socket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                stop.set()
            except Exception:
                stop.set()

        tasks = [
            asyncio.create_task(snapshot_loop()),
            asyncio.create_task(exec_loop()),
            asyncio.create_task(intr_loop()),
            asyncio.create_task(recv_loop()),
        ]

        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                await socket.close()
            except Exception:
                pass

    return app


# ---------------------------------------------------------------------------
# Programmatic uvicorn runner
# ---------------------------------------------------------------------------

async def serve(app: FastAPI, cfg: Any, stop_event: asyncio.Event) -> None:
    import uvicorn

    def _cfg_get(path: list[str], default=None):
        cur = cfg
        for p in path:
            if cur is None:
                return default
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                cur = getattr(cur, p, None)
        return default if cur is None else cur

    host = _cfg_get(["server", "host"], "127.0.0.1")
    port = int(_cfg_get(["server", "port"], 8000))

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    async def _watch() -> None:
        await stop_event.wait()
        server.should_exit = True

    watcher = asyncio.create_task(_watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
