"""Tier 1 — Executive (LLM Governor).

OWNER: Agent-Executive.

Implements:
  * OllamaClient — thin async httpx wrapper around a local Ollama instance.
  * parse_intent — natural-language -> BioPolicy via few-shot + robust JSON.
  * explain_reflex — post-hoc, Jarvis-style explanation of a reflex fire.
  * run — async loop: consume user commands, update policy, publish events.

Design notes:
  * qwen2.5:0.5b emits sloppy JSON. Strip fences, extract first balanced
    {...}, json.loads; on any failure fall back to current_policy and
    publish an ExecutiveEvent(kind="error", ...). The run loop never
    crashes on parse or transport errors.
  * Parsed JSON is a *partial* update — deep-merged into current_policy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from .contracts import BioPolicy, ExecutiveEvent, InterruptEvent
from .event_bus import ExecutiveBus, PolicyStore, Snapshot, now_ns

log = logging.getLogger("chimera.executive")


# ---------------------------------------------------------------------------
# Ollama HTTP client
# ---------------------------------------------------------------------------

class OllamaClient:
    """Minimal async client for the Ollama HTTP API.

    Uses /api/chat with stream=false so we can await the full response.
    Methods never raise for expected failures (connection, timeout, http
    error status) — they return empty string / False and log. That way
    the Executive run loop survives an Ollama outage.
    """

    def __init__(self, host: str, model: str, timeout_s: float, temperature: float) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.temperature = float(temperature)
        self._client = httpx.AsyncClient(timeout=self.timeout_s)

    async def health(self) -> bool:
        """GET /api/tags; True iff 200 and self.model is listed. 2s timeout. No raise."""
        try:
            r = await self._client.get(f"{self.host}/api/tags", timeout=2.0)
        except Exception as e:  # noqa: BLE001
            log.debug("health: request failed: %s", e)
            return False
        if r.status_code != 200:
            return False
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return False
        models = data.get("models") or []
        for m in models:
            name = m.get("name") or m.get("model") or ""
            # Ollama may or may not include the :tag; match loosely.
            if name == self.model or name.split(":")[0] == self.model.split(":")[0]:
                if name == self.model or self.model.split(":")[0] == name.split(":")[0]:
                    return True
        return False

    async def chat(self, messages: list[dict]) -> str:
        """POST /api/chat non-streaming. Returns the assistant content string.

        On transport/http error, returns "". Does not raise.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        try:
            r = await self._client.post(f"{self.host}/api/chat", json=body)
        except Exception as e:  # noqa: BLE001
            log.warning("chat request failed: %s", e)
            return ""
        if r.status_code != 200:
            log.warning("chat non-200: %s %s", r.status_code, r.text[:200])
            return ""
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("chat bad json: %s", e)
            return ""
        msg = data.get("message") or {}
        return msg.get("content") or ""

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Robust JSON extraction
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1)
    return text


def _extract_first_json_object(text: str) -> str | None:
    """Find the first balanced {...} substring, ignoring braces inside strings."""
    s = text
    n = len(s)
    i = 0
    while i < n:
        if s[i] == "{":
            depth = 0
            in_str = False
            esc = False
            for j in range(i, n):
                c = s[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return s[i:j + 1]
            return None
        i += 1
    return None


def _parse_json_loose(text: str) -> tuple[dict | None, str]:
    """Try to extract a dict from a sloppy LLM response. Returns (obj, err)."""
    if not text or not text.strip():
        return None, "empty response"
    stripped = _strip_fences(text).strip()
    candidate = _extract_first_json_object(stripped)
    if candidate is None:
        return None, "no JSON object found"
    try:
        obj = json.loads(candidate)
    except Exception as e:  # noqa: BLE001
        return None, f"json decode: {e}"
    if not isinstance(obj, dict):
        return None, "top-level JSON is not an object"
    return obj, ""


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, patch: dict) -> dict:
    """Return a new dict = base deep-merged with patch. patch wins on leaves."""
    out = dict(base)
    for k, v in patch.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Few-shot prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are the Executive governor for Project Chimera, a bio-inspired system with three reflex modules: fly (visual looming), worm (CPU/RAM pain), mouse (cursor tracking).

Your job: translate a short user command into a JSON PATCH to the BioPolicy. Output ONLY valid JSON — no prose, no markdown fences, no commentary.

BioPolicy schema (all fields optional in a patch — include ONLY fields that change):
{
  "fly":   {"sensitivity": float 0..1, "looming_threshold": float 0..1},
  "worm":  {"cpu_pain_threshold": float 0..1, "ram_pain_threshold": float 0..1, "poke_derivative": float, "dwell_ms": int},
  "mouse": {"track_target_xy": [int,int] or null, "error_threshold": float, "consecutive_frames": int}
}

Rules:
- Emit ONLY the nested fields you want to change. Do NOT re-emit unchanged fields.
- "tighten" a threshold = lower it. "loosen" / "relax" = raise it. "more sensitive" = lower threshold / higher sensitivity.
- Percentages convert to fractions (70% -> 0.70).
- If the user says nothing actionable, emit {}.

Examples:

User: tighten worm CPU pain to 70%
JSON: {"worm": {"cpu_pain_threshold": 0.70}}

User: make the fly more sensitive and drop looming threshold to 0.25
JSON: {"fly": {"sensitivity": 0.8, "looming_threshold": 0.25}}

User: track the cursor at 640,400 and require 5 consecutive frames
JSON: {"mouse": {"track_target_xy": [640, 400], "consecutive_frames": 5}}
"""


async def parse_intent(client: OllamaClient, user_text: str, current_policy: BioPolicy) -> BioPolicy:
    """Parse NL command into an updated BioPolicy via Ollama.

    On ANY failure (transport error, no JSON, bad JSON, wrong shape),
    return current_policy unchanged. Never raises.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_text.strip()},
    ]
    try:
        raw = await client.chat(messages)
    except httpx.HTTPError as e:
        log.warning("parse_intent: httpx error: %s", e)
        return current_policy
    except Exception as e:  # noqa: BLE001
        log.warning("parse_intent: unexpected error: %s", e)
        return current_policy

    patch, err = _parse_json_loose(raw)
    if patch is None:
        log.info("parse_intent: parse failed (%s); keeping current policy", err)
        return current_policy

    try:
        merged = _deep_merge(current_policy.to_dict(), patch)
        return BioPolicy.from_dict(merged)
    except Exception as e:  # noqa: BLE001
        log.warning("parse_intent: merge/from_dict failed: %s", e)
        return current_policy


# ---------------------------------------------------------------------------
# Reflex explanation
# ---------------------------------------------------------------------------

_EXPLAIN_SYSTEM = (
    "You are Jarvis narrating a reflex that just fired in Project Chimera. "
    "Reply in one short past-tense sentence, under 40 words. "
    "Explain what the module detected and why it reacted. No JSON, no preamble."
)


async def explain_reflex(client: OllamaClient, event: InterruptEvent) -> str:
    """Produce a <40-word Jarvis-style past-tense explanation."""
    ctx = {
        "module": event.module,
        "kind": event.kind,
        "payload": event.payload,
        "latency_us": event.latency_us(),
    }
    user = (
        f"Reflex fired: module={ctx['module']}, kind={ctx['kind']}, "
        f"payload={json.dumps(ctx['payload'], default=str)}. "
        "Give the one-sentence Jarvis summary."
    )
    messages = [
        {"role": "system", "content": _EXPLAIN_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        text = await client.chat(messages)
    except Exception as e:  # noqa: BLE001
        log.warning("explain_reflex: chat failed: %s", e)
        return f"{event.module} reflex fired ({event.kind})."
    text = (text or "").strip()
    if not text:
        return f"{event.module} reflex fired ({event.kind})."
    # Trim to 40 words.
    words = text.split()
    if len(words) > 40:
        text = " ".join(words[:40])
    return text


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

async def explain_and_publish(client: OllamaClient, exec_bus: ExecutiveBus, event: InterruptEvent) -> None:
    """Helper used by the action loop in main.py."""
    try:
        text = await explain_reflex(client, event)
    except Exception as e:  # noqa: BLE001
        log.warning("explain_and_publish: %s", e)
        text = f"{event.module} reflex fired ({event.kind})."
    await exec_bus.publish(ExecutiveEvent(
        t_ns=now_ns(),
        kind="explain",
        text=text,
        data={"event_kind": event.kind, "module": event.module},
    ))


async def run(
    client: OllamaClient,
    exec_bus: ExecutiveBus,
    policy_store: PolicyStore,
    command_queue: asyncio.Queue[str],
    snapshot: Snapshot,
) -> None:
    """Consume user commands forever. Never exits on its own."""
    while True:
        try:
            user_text = await command_queue.get()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("run: queue error: %s", e)
            await asyncio.sleep(0.1)
            continue

        if not user_text or not user_text.strip():
            continue

        try:
            await exec_bus.publish(ExecutiveEvent(
                t_ns=now_ns(), kind="prompt", text=user_text,
            ))
            await exec_bus.publish(ExecutiveEvent(
                t_ns=now_ns(), kind="status", text="thinking",
            ))

            current = policy_store.get()
            new_policy = await parse_intent(client, user_text, current)

            if new_policy is None:
                await exec_bus.publish(ExecutiveEvent(
                    t_ns=now_ns(), kind="error", text="parse_failed: null policy",
                ))
                new_policy = current

            try:
                await policy_store.set(new_policy)
                snapshot.policy = new_policy
            except Exception as e:  # noqa: BLE001
                await exec_bus.publish(ExecutiveEvent(
                    t_ns=now_ns(), kind="error",
                    text=f"parse_failed: policy store set: {e}",
                ))

            await exec_bus.publish(ExecutiveEvent(
                t_ns=now_ns(), kind="policy", text="", data=new_policy.to_dict(),
            ))
            await exec_bus.publish(ExecutiveEvent(
                t_ns=now_ns(), kind="status", text="idle",
            ))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("run: unhandled error: %s", e)
            try:
                await exec_bus.publish(ExecutiveEvent(
                    t_ns=now_ns(), kind="error", text=f"executive_error: {e}",
                ))
            except Exception:  # noqa: BLE001
                pass
            # Keep looping.
            continue
