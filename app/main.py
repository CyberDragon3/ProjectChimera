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


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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

    # Tier 1
    ollama = exec_layer.OllamaClient(
        host=cfg["ollama"]["host"],
        model=cfg["ollama"]["model"],
        timeout_s=cfg["ollama"]["timeout_s"],
        temperature=cfg["ollama"]["temperature"],
    )
    if not await ollama.health():
        log.warning("Ollama health check failed — Executive will retry on each call.")

    # Dashboard / command-bar server
    from .dashboard.server import build_app, serve
    app = build_app(snapshot, policy_store, exec_bus, interrupt_bus, command_queue, cfg)

    tasks = [
        asyncio.create_task(trans_layer.run(stim_bus, cfg, stop_event, snapshot), name="translation"),
        asyncio.create_task(fly_mod.run(stim_bus, interrupt_bus, policy_store, snapshot, stop_event), name="fly"),
        asyncio.create_task(worm_mod.run(stim_bus, interrupt_bus, policy_store, snapshot, stop_event), name="worm"),
        asyncio.create_task(mouse_mod.run(stim_bus, interrupt_bus, policy_store, snapshot, stop_event), name="mouse"),
        asyncio.create_task(exec_layer.run(ollama, exec_bus, policy_store, command_queue, snapshot), name="executive"),
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
    EXPLAIN_COOLDOWN_NS = 4 * 1_000_000_000
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
