"""FastAPI dashboard — /events SSE stream + /state snapshot."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from chimera.bus import Bus
from chimera.store import RingBuffer

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(bus: Bus, thermal_buf: RingBuffer, history_size: int = 200) -> FastAPI:
    recent: deque[dict] = deque(maxlen=history_size)

    async def _record() -> None:
        q = bus.subscribe("")  # root prefix — receive ALL events
        try:
            while True:
                ev = await q.get()
                recent.append({"topic": ev.topic, "payload": ev.payload, "ts": ev.ts})
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
        }

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            q = bus.subscribe("")
            try:
                while True:
                    ev = await q.get()
                    data = json.dumps({"topic": ev.topic, "payload": ev.payload, "ts": ev.ts})
                    yield f"data: {data}\n\n".encode()
            finally:
                bus.unsubscribe("", q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
