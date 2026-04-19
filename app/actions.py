"""Reflex action handlers.

Map InterruptEvent.kind to a handler. All destructive actions are dry-run:
they publish a log line and do NOT mutate real system state unless the
config explicitly allows it AND the user has opted in.
"""
from __future__ import annotations

import logging
import os
import signal
from typing import Any, Awaitable, Callable

from .contracts import InterruptEvent
from .event_bus import ExecutiveBus, Snapshot, now_ns
from .contracts import ExecutiveEvent

log = logging.getLogger("chimera.actions")

Handler = Callable[[InterruptEvent, dict[str, Any], ExecutiveBus, Snapshot], Awaitable[None]]


async def _publish(exec_bus: ExecutiveBus, snapshot: Snapshot, text: str) -> None:
    event = ExecutiveEvent(t_ns=now_ns(), kind="status", text=text)
    snapshot.recent_executive.append(event)
    await exec_bus.publish(event)


async def handle_kill_process(
    event: InterruptEvent,
    cfg: dict[str, Any],
    exec_bus: ExecutiveBus,
    snapshot: Snapshot,
) -> None:
    """AVA recoil → kill notification with self-preservation and context awareness."""
    dry = cfg.get("actions", {}).get("kill_process_dry_run", True)
    protected_processes = {"python", "ollama", "code", "vscode", "cmd", "wt", "explorer", "cursor"}
    
    payload = event.payload
    cpu = payload.get("cpu", 0.0)
    ram = payload.get("ram", 0.0)
    process_name = payload.get("process_name", "").lower()
    
    # Priority 1: Check if the system is actually in pain based on config
    if cpu < 0.85 and ram < 0.90:
        return # Silent return; thresholds not met

    # Priority 2: Self-Preservation (Don't kill the brain or the tools)
    if process_name and any(proc in process_name for proc in protected_processes):
        msg = f"[REFLEX] worm: pain detected (cpu={cpu:.2f}) but '{process_name}' is PROTECTED. Skipping."
        log.warning(msg)
        await _publish(exec_bus, snapshot, msg)
        return

    # Priority 3: Identify the Target (Active Shell PID vs General Culprit)
    active_pid = getattr(snapshot, "active_shell_pid", None)
    target_pid = active_pid if active_pid else int(payload.get("pid", 0))

    if dry:
        mode_str = "DRY-RUN"
        msg = f"[REFLEX] worm {mode_str}: pain from '{process_name}' (PID {target_pid}) at cpu={cpu:.2f}. No action taken."
        log.warning(msg)
        await _publish(exec_bus, snapshot, msg)
        return

    # Priority 4: Execution
    if target_pid > 0:
        try:
            os.kill(target_pid, signal.SIGTERM)
            msg = f"[REFLEX] worm LIVE: Neutralized '{process_name}' (PID {target_pid}) to relieve system pain."
            # Clear the shell PID tracker if we just killed it
            if target_pid == active_pid:
                snapshot.active_shell_pid = None
        except Exception as e:
            msg = f"[REFLEX] worm ERROR: Failed to kill PID {target_pid}: {str(e)}"
    else:
        msg = f"[REFLEX] worm: Pain detected but no valid PID found for '{process_name}'."

    log.warning(msg)
    await _publish(exec_bus, snapshot, msg)


async def handle_snap_cursor(
    event: InterruptEvent,
    cfg: dict[str, Any],
    exec_bus: ExecutiveBus,
    snapshot: Snapshot,
) -> None:
    """Fly looming handler."""
    msg = f"[REFLEX] fly looming fired (flow={event.payload.get('flow'):.2f})"
    log.warning(msg)
    await _publish(exec_bus, snapshot, msg)


async def handle_error_spike(
    event: InterruptEvent,
    cfg: dict[str, Any],
    exec_bus: ExecutiveBus,
    snapshot: Snapshot,
) -> None:
    """Mouse error spike handler."""
    msg = f"[REFLEX] mouse cortex error spike (err={event.payload.get('error'):.1f} px)"
    log.warning(msg)
    await _publish(exec_bus, snapshot, msg)


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
    await h(event, cfg, exec_bus, snapshot)
    event.t_action_ns = now_ns()
    snapshot.recent_interrupts.append(event)