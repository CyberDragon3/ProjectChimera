"""Project Chimera orchestrator — Phase 2 wires this up."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from . import (
    actions,
    tier1_executive as exec_layer,
    tier2_translation as trans_layer,
)
from .contracts import BioPolicy
from .event_bus import ExecutiveBus, InterruptBus, PolicyStore, Snapshot, StimulusBus
from .tier3_reflex import fly as fly_mod, worm as worm_mod, mouse as mouse_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chimera.main")

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (patch or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict[str, Any]:
    """Load bundled defaults, then deep-merge the user config on top.

    The wizard only writes the sections it owns (``llm``, ``tools``…) so we
    always rehydrate the rest from the bundled defaults shipped inside the
    PyInstaller archive. Otherwise a partial user config would trigger
    ``KeyError`` for untouched sections like ``policy`` / ``server``.
    """
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}

    try:
        from .setup_check import user_config_path
        user_path = user_config_path()
    except Exception:
        user_path = None

    if user_path and user_path.exists() and user_path.resolve() != CONFIG_PATH.resolve():
        try:
            with user_path.open("r", encoding="utf-8") as f:
                patch = yaml.safe_load(f) or {}
            return _deep_merge(base, patch)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to merge user config at %s: %s", user_path, exc)

    return base


async def run_app() -> None:
    cfg = load_config()

    policy = BioPolicy.from_dict(cfg["policy"])
    policy_store = PolicyStore(policy)
    stim_bus = StimulusBus()
    interrupt_bus = InterruptBus()
    exec_bus = ExecutiveBus()
    snapshot = Snapshot(policy=policy)
    command_queue: asyncio.Queue[str] = asyncio.Queue()
    stop_event = asyncio.Event()

    ollama = exec_layer.LLMClientProxy(cfg)
    if not await ollama.health():
        log.warning("LLM health check failed — Executive will retry on each call.")

    from .dashboard.server import build_app, serve
    app = build_app(
        snapshot, policy_store, exec_bus, interrupt_bus, command_queue, cfg,
        llm_proxy=ollama,
    )
    cfg["_exec_bus"] = exec_bus
    cfg["_snapshot"] = snapshot

    tasks = [
        asyncio.create_task(trans_layer.run(stim_bus, cfg, stop_event, snapshot), name="translation"),
        asyncio.create_task(fly_mod.run(stim_bus, interrupt_bus, policy_store, snapshot, stop_event), name="fly"),
        asyncio.create_task(worm_mod.run(stim_bus, interrupt_bus, policy_store, snapshot, stop_event), name="worm"),
        asyncio.create_task(mouse_mod.run(stim_bus, interrupt_bus, policy_store, snapshot, stop_event), name="mouse"),
        asyncio.create_task(exec_layer.run(ollama, exec_bus, policy_store, command_queue, snapshot, cfg), name="executive"),
        asyncio.create_task(_action_loop(interrupt_bus, cfg, exec_bus, ollama, snapshot), name="actions"),
        asyncio.create_task(serve(app, cfg, stop_event), name="server"),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        stop_event.set()

async def _action_loop(interrupt_bus: InterruptBus, cfg: dict[str, Any], exec_bus: ExecutiveBus,
                       ollama: Any, snapshot: Snapshot) -> None:
    """Consume reflex fires; dispatch the action; call the LLM for a
    narration — but throttle per-module so a chattering connectome doesn't
    stack up Ollama requests (which would then themselves spike CPU and
    re-trigger the worm). One explanation per module per 4 seconds."""
    from .contracts import ExecutiveEvent
    from .event_bus import now_ns
    EXPLAIN_COOLDOWN_NS = 30 * 1_000_000_000
    last_explain_ns: dict[str, int] = {}
    while True:
        event = await interrupt_bus.main.get()
        await actions.dispatch(event, cfg, exec_bus, snapshot)
        t = now_ns()
        if t - last_explain_ns.get(event.module, 0) < EXPLAIN_COOLDOWN_NS:
            continue
        last_explain_ns[event.module] = t
        try:
            explanation = await exec_layer.explain_reflex(ollama, event)
            await exec_bus.publish(ExecutiveEvent(
                t_ns=now_ns(), kind="explain", text=explanation,
                data={"event_kind": event.kind, "module": event.module}))
        except Exception as e:  # noqa: BLE001
            log.warning("explain_reflex failed: %s", e)


if __name__ == "__main__":
    try:
        asyncio.run(run_app())
    except KeyboardInterrupt:
        log.info("shutting down")
