"""Reflex action handlers.

Map InterruptEvent.kind to a handler. All destructive actions are dry-run:
they publish a log line and do NOT mutate real system state unless the
config explicitly allows it AND the user has opted in.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .contracts import InterruptEvent
from .event_bus import ExecutiveBus, Snapshot, now_ns
from .contracts import ExecutiveEvent

log = logging.getLogger("chimera.actions")

Handler = Callable[[InterruptEvent, dict[str, Any], ExecutiveBus], Awaitable[None]]


async def handle_kill_process(event: InterruptEvent, cfg: dict[str, Any], exec_bus: ExecutiveBus) -> None:
    """AVA recoil → dry-run kill notification."""
    dry = cfg.get("actions", {}).get("kill_process_dry_run", True)
    msg = (
        f"[REFLEX] worm AVA recoil fired (cpu={event.payload.get('cpu'):.2f} "
        f"ram={event.payload.get('ram'):.2f}) — "
        + ("dry-run: no process killed" if dry else "LIVE: kill disabled in MVP")
    )
    log.warning(msg)
    await exec_bus.publish(ExecutiveEvent(t_ns=now_ns(), kind="status", text=msg))


async def handle_snap_cursor(event: InterruptEvent, cfg: dict[str, Any], exec_bus: ExecutiveBus) -> None:
    """Fly looming → no cursor snap in MVP; log only (pynput import deferred
    by Agent-Translation). Extend here if desired."""
    msg = f"[REFLEX] fly looming fired (flow={event.payload.get('flow'):.2f})"
    log.warning(msg)
    await exec_bus.publish(ExecutiveEvent(t_ns=now_ns(), kind="status", text=msg))


async def handle_error_spike(event: InterruptEvent, cfg: dict[str, Any], exec_bus: ExecutiveBus) -> None:
    msg = f"[REFLEX] mouse cortex error spike (err={event.payload.get('error'):.1f} px)"
    log.warning(msg)
    await exec_bus.publish(ExecutiveEvent(t_ns=now_ns(), kind="status", text=msg))


HANDLERS: dict[str, Handler] = {
    "ava_recoil": handle_kill_process,
    "looming": handle_snap_cursor,
    "error_spike": handle_error_spike,
}


async def dispatch(event: InterruptEvent, cfg: dict[str, Any], exec_bus: ExecutiveBus, snapshot: Snapshot) -> None:
    h = HANDLERS.get(event.kind)
    if h is None:
        log.info("No handler for interrupt kind=%s", event.kind)
        return
    await h(event, cfg, exec_bus)
    event.t_action_ns = now_ns()
    snapshot.recent_interrupts.append(event)
