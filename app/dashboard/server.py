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

    recent_executive = []
    for ev in list(getattr(snapshot, "recent_executive", []) or []):
        recent_executive.append(_serialize_executive(ev))

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
        "recent_executive": recent_executive,
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(
    snapshot, policy_store, exec_bus, interrupt_bus, command_queue, cfg,
    llm_proxy=None,
) -> FastAPI:
    """Build the FastAPI app.

    ``llm_proxy`` is the shared ``tier1_executive.LLMClientProxy`` held by
    the executive/action-loop tasks. When the onboarding wizard saves a
    new provider we ask the proxy to rebuild its inner client so the UX
    goes from "wizard finished" → "live Jarvis on the new provider"
    without a process restart.
    """
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

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page() -> Any:
        path = STATIC_DIR / "settings.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="settings.html not found")
        return FileResponse(str(path))

    def _current_llm_cfg() -> dict:
        """Preferred: cfg.llm; fallback: synthesize from legacy cfg.ollama."""
        llm = _cfg_get(["llm"]) or {}
        if not llm or not llm.get("provider"):
            llm = {
                "provider": "ollama",
                "model": _cfg_get(["ollama", "model"], "qwen2.5:0.5b"),
                "host": _cfg_get(["ollama", "host"], "http://localhost:11434"),
                "timeout_s": _cfg_get(["ollama", "timeout_s"], 30.0),
                "temperature": _cfg_get(["ollama", "temperature"], 0.1),
            }
        return dict(llm)

    def _redact(cfg: dict) -> dict:
        """Return an llm cfg dict with api_key masked for display."""
        out = dict(cfg or {})
        key = out.get("api_key") or ""
        if key:
            out["api_key_masked"] = setup_check._mask_key(key)
            out["api_key_set"] = True
        else:
            out["api_key_masked"] = ""
            out["api_key_set"] = False
        out.pop("api_key", None)
        return out

    @app.get("/api/setup/status")
    async def setup_status() -> dict:
        llm = _current_llm_cfg()
        provider = str(llm.get("provider") or "ollama")
        payload: dict[str, Any] = {
            "marker": setup_check.is_complete(),
            "provider": provider,
            "llm": _redact(llm),
            "config_path": str(setup_check.user_config_path()),
            "config_exists": setup_check.user_config_path().exists(),
        }

        if provider == "ollama":
            host = str(llm.get("host") or "http://localhost:11434")
            model = str(llm.get("model") or "qwen2.5:0.5b")
            ollama = await setup_check.check_ollama(host)
            model_info = {"present": False, "size_bytes": None, "model": model}
            if ollama.get("reachable"):
                model_info = await setup_check.check_model(host, model)
            payload["ollama"] = ollama
            payload["model"] = model_info
        elif provider == "anthropic":
            probe = await setup_check.check_anthropic(
                str(llm.get("api_key") or ""),
                str(llm.get("model") or ""),
            )
            payload["cloud"] = probe
        else:  # openai / openai_compat
            probe = await setup_check.check_openai(
                str(llm.get("api_key") or ""),
                str(llm.get("base_url") or "https://api.openai.com/v1"),
                str(llm.get("model") or ""),
            )
            payload["cloud"] = probe

        return payload

    @app.post("/api/setup/test_provider")
    async def setup_test_provider(body: dict) -> dict:
        """Probe the given provider configuration *without* persisting it.

        Body shape: ``{"provider": "...", "model": "...", "api_key": "...",
        "base_url": "...", "host": "..."}``.
        """
        body = body or {}
        provider = str(body.get("provider") or "ollama").lower()
        model = str(body.get("model") or "")

        if provider == "ollama":
            host = str(body.get("host") or "http://localhost:11434")
            ollama = await setup_check.check_ollama(host)
            model_info = None
            if ollama.get("reachable") and model:
                model_info = await setup_check.check_model(host, model)
            ok = bool(ollama.get("reachable"))
            return {
                "ok": ok,
                "provider": provider,
                "ollama": ollama,
                "model": model_info,
            }

        if provider == "anthropic":
            probe = await setup_check.check_anthropic(
                str(body.get("api_key") or ""), model or None,
            )
            return {
                "ok": bool(probe.get("authenticated")),
                "provider": provider,
                "cloud": probe,
            }

        # openai / openai_compat
        probe = await setup_check.check_openai(
            str(body.get("api_key") or ""),
            str(body.get("base_url") or "https://api.openai.com/v1"),
            model or None,
        )
        return {
            "ok": bool(probe.get("authenticated")),
            "provider": provider,
            "cloud": probe,
        }

    @app.post("/api/setup/save_provider")
    async def setup_save_provider(body: dict) -> dict:
        """Persist a provider configuration to the writable user config.

        Body: ``{"provider", "model", "api_key"?, "base_url"?, "host"?,
        "temperature"?, "timeout_s"?, "max_tokens"?}``. Existing fields
        are deep-merged — unrelated sections of the config are preserved.
        """
        body = body or {}
        provider = str(body.get("provider") or "").lower()
        if provider not in ("ollama", "openai", "anthropic", "openai_compat"):
            raise HTTPException(status_code=400, detail=f"unknown provider: {provider}")

        llm_patch: dict[str, Any] = {
            "provider": provider,
            "model": str(body.get("model") or ""),
        }
        for optional in ("temperature", "timeout_s", "max_tokens"):
            if optional in body and body[optional] is not None:
                llm_patch[optional] = body[optional]

        if provider == "ollama":
            llm_patch["host"] = str(body.get("host") or "http://localhost:11434")
        else:
            api_key = body.get("api_key")
            if api_key:  # only overwrite when a new value is supplied
                llm_patch["api_key"] = str(api_key)
            if provider in ("openai", "openai_compat"):
                default_base = (
                    "https://api.openai.com/v1" if provider == "openai"
                    else "http://localhost:8080/v1"
                )
                llm_patch["base_url"] = str(body.get("base_url") or default_base)

        user_cfg = setup_check.load_user_config()
        merged = setup_check.deep_merge(user_cfg, {"llm": llm_patch})

        # Keep legacy ollama mirror in sync when provider is ollama so existing
        # code paths that still read ollama.host / ollama.model stay valid.
        if provider == "ollama":
            merged = setup_check.deep_merge(merged, {
                "ollama": {
                    "host": llm_patch["host"],
                    "model": llm_patch["model"],
                }
            })

        path = setup_check.save_user_config(merged)

        # Hot-reload the LLM so the user doesn't have to restart after the
        # wizard. If no proxy was passed (legacy test harness), fall back to
        # the old "restart required" semantics.
        reloaded = False
        if llm_proxy is not None:
            try:
                await llm_proxy.reload(merged)
                reloaded = bool(await llm_proxy.health())
            except Exception as exc:  # noqa: BLE001
                log.warning("llm reload failed: %s", exc)

        return {
            "ok": True,
            "config_path": str(path),
            "restart_required": not reloaded,
            "reloaded": reloaded,
            "llm": _redact(merged.get("llm") or {}),
        }

    @app.post("/api/setup/pull_model")
    async def setup_pull_model(body: dict | None = None) -> StreamingResponse:
        body = body or {}
        host = str(body.get("host") or _cfg_get(["llm", "host"]) or _cfg_get(["ollama", "host"], "http://localhost:11434"))
        model = str(body.get("model") or _cfg_get(["llm", "model"]) or _cfg_get(["ollama", "model"], "qwen2.5:0.5b"))

        async def gen():
            async for ev in setup_check.stream_pull(host, model):
                yield (json.dumps(ev) + "\n").encode("utf-8")

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.post("/api/setup/mark_complete")
    async def setup_mark_complete() -> dict:
        setup_check.mark_complete()
        return {"ok": True, "redirect": "/"}

    @app.post("/api/setup/reset")
    async def setup_reset() -> dict:
        """Forget the first-run marker so the wizard runs again on next load."""
        setup_check.unmark()
        return {"ok": True, "redirect": "/setup"}

    @app.get("/api/settings")
    async def settings_get() -> dict:
        """Return the merged runtime config with secrets redacted for the
        settings UI. Includes both the active (merged) config and the
        user-layer overrides so the UI can tell what was customized."""
        llm = _current_llm_cfg()
        return {
            "llm": _redact(llm),
            "user_config_path": str(setup_check.user_config_path()),
            "user_config_exists": setup_check.user_config_path().exists(),
            "marker": setup_check.is_complete(),
        }

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
        """Transcribe an uploaded audio blob via an OpenAI-compatible
        ``/audio/transcriptions`` endpoint.

        Works with OpenAI directly (whisper-1) and with any OpenAI-compatible
        host that exposes Whisper (e.g. Groq's ``whisper-large-v3-turbo``).
        For Anthropic or Ollama the caller can set ``voice.base_url`` /
        ``voice.api_key`` / ``voice.model`` in the user config to route voice
        through a different provider than the chat LLM.
        """
        blob = await file.read()
        if not blob:
            raise HTTPException(status_code=400, detail="empty upload")

        llm = _current_llm_cfg()
        voice = _cfg_get(["voice"]) or {}

        api_key = str(voice.get("api_key") or "").strip()
        base_url = str(voice.get("base_url") or "").strip()
        model = str(voice.get("model") or "whisper-1").strip()

        # Auto-inherit from the chat LLM when voice is not explicitly configured.
        if not api_key or not base_url:
            provider = str(llm.get("provider") or "").lower()
            if provider in ("openai", "openai_compat") and llm.get("api_key"):
                api_key = api_key or str(llm["api_key"])
                base_url = base_url or str(llm.get("base_url") or "https://api.openai.com/v1")

        if not api_key or not base_url:
            return {
                "ok": False,
                "text": "",
                "error": "Voice transcription needs an OpenAI-compatible key. Add one in Settings or type your command instead.",
            }

        import httpx  # local import keeps the module optional for tests

        filename = file.filename or "audio.webm"
        mime = file.content_type or "audio/webm"
        url = base_url.rstrip("/") + "/audio/transcriptions"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (filename, blob, mime)},
                    data={"model": model, "response_format": "json"},
                )
        except httpx.HTTPError as exc:
            return {"ok": False, "text": "", "error": f"transcription network error: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "error": f"transcription error: {exc}"}

        if r.status_code != 200:
            detail = (r.text or "")[:200]
            return {
                "ok": False,
                "text": "",
                "error": f"transcription HTTP {r.status_code}: {detail}",
            }

        try:
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "error": f"transcription bad json: {exc}"}

        text = str(data.get("text") or "").strip()
        return {"ok": True, "text": text, "model": model}

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
