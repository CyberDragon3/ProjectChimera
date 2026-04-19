"""FastAPI dashboard — /events SSE stream + /state snapshot."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from chimera.bus import Bus
from chimera.store import RingBuffer

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(bus: Bus, thermal_buf: RingBuffer, history_size: int = 200) -> FastAPI:
    recent: deque[dict] = deque(maxlen=history_size)
    neuro_state: dict[str, dict[str, Any] | None] = {
        "dopamine": {"level": 0.0, "hit_rate": 0.5, "last_outcome": None, "ts": None},
        "mouse_rate": {"e_rate_hz": 0.0, "i_rate_hz": 0.0, "ts": None},
        "last_zebrafish_spike": None,
        "last_fly_spike": None,
    }

    async def _record() -> None:
        q = bus.subscribe("")  # root prefix — receive ALL events
        try:
            while True:
                ev = await q.get()
                recent.append({"topic": ev.topic, "payload": ev.payload, "ts": ev.ts})
                if ev.topic == "neuro.dopamine":
                    neuro_state["dopamine"] = {**ev.payload, "ts": ev.ts}
                elif ev.topic == "neuro.mouse.rate":
                    neuro_state["mouse_rate"] = {**ev.payload, "ts": ev.ts}
                elif ev.topic == "neuro.zebrafish.spike":
                    neuro_state["last_zebrafish_spike"] = {**ev.payload, "ts": ev.ts}
                elif ev.topic == "neuro.fly.spike":
                    neuro_state["last_fly_spike"] = {**ev.payload, "ts": ev.ts}
        finally:
            bus.unsubscribe("", q)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_record())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Chimera Dashboard", lifespan=lifespan)

    @app.get("/state")
    async def state() -> dict:
        latest = thermal_buf.latest()
        slope = thermal_buf.slope(60)
        return {
            "recent_events": list(recent)[-50:],
            "thermal": {
                "latest_c": latest.v if latest else None,
                "slope_c_per_min": slope * 60,
                "samples": len(thermal_buf),
            },
            "neuro": neuro_state,
        }

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            q = bus.subscribe("")
            try:
                while True:
                    try:
                        # Periodic keepalive so dropped connections surface fast
                        # via CancelledError instead of silently wedging the queue.
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    data = json.dumps({"topic": ev.topic, "payload": ev.payload, "ts": ev.ts})
                    yield f"data: {data}\n\n".encode()
            except asyncio.CancelledError:
                log.info("dashboard.sse.client_disconnected")
                raise
            finally:
                bus.unsubscribe("", q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
