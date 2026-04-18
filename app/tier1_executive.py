"""Tier 1 — Executive (LLM Governor).

OWNER: Agent-Executive.

Implements:
  * ``LLMClient`` protocol (async ``health`` / ``chat`` / ``aclose``) with
    concrete backends for Ollama, OpenAI-compatible endpoints, and Anthropic.
  * ``build_llm_client(cfg)`` — factory that reads ``cfg["llm"]["provider"]``.
  * parse_intent — natural-language -> BioPolicy via few-shot + robust JSON.
  * explain_reflex — post-hoc, Jarvis-style explanation of a reflex fire.
  * run — async loop: consume user commands, update policy, publish events.

Design notes:
  * Small local models emit sloppy JSON. Strip fences, extract first balanced
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
from typing import Any, Protocol

import httpx

from .contracts import BioPolicy, ExecutiveEvent, InterruptEvent
from .event_bus import ExecutiveBus, PolicyStore, Snapshot, now_ns

log = logging.getLogger("chimera.executive")


# ---------------------------------------------------------------------------
# LLM client protocol
# ---------------------------------------------------------------------------

class LLMClient(Protocol):
    model: str

    async def health(self) -> bool: ...
    async def chat(self, messages: list[dict]) -> str: ...
    async def aclose(self) -> None: ...


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
# OpenAI-compatible client (OpenAI, Groq, Together, OpenRouter, local llama.cpp)
# ---------------------------------------------------------------------------

class OpenAIClient:
    """OpenAI /chat/completions client. Works against any OpenAI-compatible
    endpoint via ``base_url``. ``chat`` returns the assistant content or ""
    on failure (never raises)."""

    def __init__(self, api_key: str, model: str, base_url: str,
                 timeout_s: float, temperature: float) -> None:
        self.api_key = api_key or ""
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.timeout_s = float(timeout_s)
        self.temperature = float(temperature)
        self._client = httpx.AsyncClient(timeout=self.timeout_s)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def health(self) -> bool:
        if not self.api_key:
            return False
        try:
            r = await self._client.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=3.0,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("openai health failed: %s", e)
            return False
        return 200 <= r.status_code < 400

    async def chat(self, messages: list[dict]) -> str:
        if not self.api_key:
            log.warning("openai chat: no api key configured")
            return ""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        try:
            r = await self._client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("openai chat request failed: %s", e)
            return ""
        if r.status_code != 200:
            log.warning("openai chat non-200: %s %s", r.status_code, r.text[:200])
            return ""
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("openai chat bad json: %s", e)
            return ""
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return msg.get("content") or ""

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

class AnthropicClient:
    """Anthropic /v1/messages client. ``chat`` accepts OpenAI-style messages
    (with an optional leading system message) and returns the first text
    block's content."""

    def __init__(self, api_key: str, model: str, timeout_s: float,
                 temperature: float, max_tokens: int = 1024) -> None:
        self.api_key = api_key or ""
        self.model = model
        self.timeout_s = float(timeout_s)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self._client = httpx.AsyncClient(timeout=self.timeout_s)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    async def health(self) -> bool:
        if not self.api_key:
            return False
        try:
            r = await self._client.get(
                "https://api.anthropic.com/v1/models",
                headers=self._headers(),
                timeout=3.0,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("anthropic health failed: %s", e)
            return False
        return 200 <= r.status_code < 400

    async def chat(self, messages: list[dict]) -> str:
        if not self.api_key:
            log.warning("anthropic chat: no api key configured")
            return ""

        system_parts: list[str] = []
        convo: list[dict] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                if content:
                    system_parts.append(content)
            elif role in ("user", "assistant"):
                convo.append({"role": role, "content": content})

        if not convo:
            convo = [{"role": "user", "content": ""}]

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": convo,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)

        try:
            r = await self._client.post(
                "https://api.anthropic.com/v1/messages",
                headers=self._headers(),
                json=body,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("anthropic chat request failed: %s", e)
            return ""
        if r.status_code != 200:
            log.warning("anthropic chat non-200: %s %s", r.status_code, r.text[:200])
            return ""
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("anthropic chat bad json: %s", e)
            return ""
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text") or ""
        return ""

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_llm_client(cfg: dict) -> LLMClient:
    """Build the configured LLM client. Falls back to Ollama.

    Config shape::

        llm:
          provider: ollama | openai | anthropic | openai_compat
          model: "..."
          api_key: "..."              # cloud providers
          base_url: "..."             # openai_compat / azure / local
          timeout_s: 30.0
          temperature: 0.1
          max_tokens: 1024            # anthropic

        ollama:
          host: "http://localhost:11434"
          model: "qwen2.5:0.5b"

    The legacy top-level ``ollama`` block is honored when
    ``llm.provider == "ollama"`` and fields are missing from ``llm``.
    """
    llm_cfg = dict(cfg.get("llm") or {})
    provider = str(llm_cfg.get("provider") or "ollama").lower()

    ollama_cfg = dict(cfg.get("ollama") or {})

    temperature = float(llm_cfg.get("temperature", ollama_cfg.get("temperature", 0.1)))
    timeout_s = float(llm_cfg.get("timeout_s", ollama_cfg.get("timeout_s", 30.0)))

    if provider == "ollama":
        host = str(llm_cfg.get("host") or ollama_cfg.get("host") or "http://localhost:11434")
        model = str(llm_cfg.get("model") or ollama_cfg.get("model") or "qwen2.5:0.5b")
        return OllamaClient(host=host, model=model, timeout_s=timeout_s, temperature=temperature)

    if provider in ("openai", "openai_compat"):
        base_url = str(
            llm_cfg.get("base_url")
            or ("https://api.openai.com/v1" if provider == "openai" else "http://localhost:8080/v1")
        )
        model = str(llm_cfg.get("model") or "gpt-4o-mini")
        api_key = str(llm_cfg.get("api_key") or "")
        return OpenAIClient(
            api_key=api_key, model=model, base_url=base_url,
            timeout_s=timeout_s, temperature=temperature,
        )

    if provider == "anthropic":
        model = str(llm_cfg.get("model") or "claude-3-5-haiku-latest")
        api_key = str(llm_cfg.get("api_key") or "")
        max_tokens = int(llm_cfg.get("max_tokens", 1024))
        return AnthropicClient(
            api_key=api_key, model=model, timeout_s=timeout_s,
            temperature=temperature, max_tokens=max_tokens,
        )

    log.warning("unknown llm.provider=%r; falling back to ollama", provider)
    host = str(ollama_cfg.get("host") or "http://localhost:11434")
    model = str(ollama_cfg.get("model") or "qwen2.5:0.5b")
    return OllamaClient(host=host, model=model, timeout_s=timeout_s, temperature=temperature)


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


async def parse_intent(client: LLMClient, user_text: str, current_policy: BioPolicy) -> BioPolicy:
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


async def explain_reflex(client: LLMClient, event: InterruptEvent) -> str:
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
# Policy narration
# ---------------------------------------------------------------------------

def _policy_change_fragments(before: BioPolicy, after: BioPolicy) -> list[str]:
    changes: list[str] = []

    if before.fly.sensitivity != after.fly.sensitivity:
        changes.append(f"fly sensitivity to {after.fly.sensitivity:.2f}")
    if before.fly.looming_threshold != after.fly.looming_threshold:
        changes.append(f"fly looming threshold to {after.fly.looming_threshold:.2f}")

    if before.worm.cpu_pain_threshold != after.worm.cpu_pain_threshold:
        changes.append(
            f"worm CPU pain threshold to {after.worm.cpu_pain_threshold * 100:.0f} percent"
        )
    if before.worm.ram_pain_threshold != after.worm.ram_pain_threshold:
        changes.append(
            f"worm RAM pain threshold to {after.worm.ram_pain_threshold * 100:.0f} percent"
        )
    if before.worm.poke_derivative != after.worm.poke_derivative:
        changes.append(f"worm poke derivative to {after.worm.poke_derivative:.2f}")
    if before.worm.dwell_ms != after.worm.dwell_ms:
        changes.append(f"worm dwell to {after.worm.dwell_ms} milliseconds")

    if before.mouse.track_target_xy != after.mouse.track_target_xy:
        if after.mouse.track_target_xy is None:
            changes.append("mouse target cleared")
        else:
            x, y = after.mouse.track_target_xy
            changes.append(f"mouse target to {x}, {y}")
    if before.mouse.error_threshold != after.mouse.error_threshold:
        changes.append(f"mouse error threshold to {after.mouse.error_threshold:.0f} pixels")
    if before.mouse.consecutive_frames != after.mouse.consecutive_frames:
        frame_word = "frame" if after.mouse.consecutive_frames == 1 else "frames"
        changes.append(
            f"mouse confirmation to {after.mouse.consecutive_frames} {frame_word}"
        )

    return changes

def describe_policy_change(before: BioPolicy, after: BioPolicy) -> str:
    changes = _policy_change_fragments(before, after)
    if not changes:
        return (
            "I kept the current reflex policy. Name the fly, worm, or mouse module "
            "to change a threshold."
        )
    if len(changes) == 1:
        detail = changes[0]
    elif len(changes) == 2:
        detail = f"{changes[0]} and {changes[1]}"
    else:
        detail = ", ".join(changes[:-1]) + f", and {changes[-1]}"
    return f"Understood. Updated {detail}."

async def publish_event(
    exec_bus: ExecutiveBus,
    snapshot: Snapshot,
    *,
    kind: str,
    text: str = "",
    data: dict[str, Any] | None = None,
) -> None:
    event = ExecutiveEvent(
        t_ns=now_ns(),
        kind=kind,
        text=text,
        data=data or {},
    )
    snapshot.recent_executive.append(event)
    await exec_bus.publish(event)


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

async def explain_and_publish(
    client: LLMClient,
    exec_bus: ExecutiveBus,
    snapshot: Snapshot,
    event: InterruptEvent,
) -> None:
    """Helper used by the action loop in main.py."""
    try:
        text = await explain_reflex(client, event)
    except Exception as e:  # noqa: BLE001
        log.warning("explain_and_publish: %s", e)
        text = f"{event.module} reflex fired ({event.kind})."
    await publish_event(
        exec_bus,
        snapshot,
        kind="explain",
        text=text,
        data={"event_kind": event.kind, "module": event.module},
    )


_OPEN_PREFIXES = ("open ", "launch ", "start ", "bring up ")
_SEARCH_PREFIXES = ("search ", "google ", "look up ")
_VISIT_PREFIXES = ("go to ", "visit ")

_SITE_ALIASES = {
    "github": "https://github.com/",
    "gmail": "https://mail.google.com/",
    "google": "https://www.google.com/",
    "linkedin": "https://www.linkedin.com/",
    "netflix": "https://www.netflix.com/",
    "openai": "https://openai.com/",
    "reddit": "https://www.reddit.com/",
    "spotify": "https://open.spotify.com/",
    "twitter": "https://x.com/",
    "x": "https://x.com/",
    "youtube": "https://www.youtube.com/",
}

_APP_ALIASES = {
    "calculator": "calculator",
    "chrome": "chrome",
    "command prompt": "cmd",
    "edge": "edge",
    "file explorer": "explorer",
    "firefox": "firefox",
    "google chrome": "chrome",
    "microsoft edge": "edge",
    "notepad": "notepad",
    "spotify": "spotify",
    "terminal": "terminal",
    "visual studio code": "vscode",
    "vs code": "vscode",
    "vscode": "vscode",
    "windows terminal": "terminal",
}


def _normalize_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9./:+-]+", " ", (text or "").lower())).strip()


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
    stripped = (text or "").strip()
    lower = stripped.lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            return stripped[len(prefix):].strip()
    return None


def _safe_apps(cfg: dict) -> dict[str, str]:
    return dict(((cfg.get("tools") or {}).get("safe_apps") or {}))


def _match_safe_app(target: str, cfg: dict) -> str | None:
    safe_apps = _safe_apps(cfg)
    if not safe_apps:
        return None

    normalized = _normalize_phrase(target)
    candidates = [normalized]
    alias = _APP_ALIASES.get(normalized)
    if alias:
        candidates.append(alias)

    for candidate in candidates:
        if candidate in safe_apps:
            return candidate
    return None


def _match_url_target(target: str) -> str | None:
    raw = (target or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw

    normalized = _normalize_phrase(raw)
    if normalized in _SITE_ALIASES:
        return _SITE_ALIASES[normalized]

    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+([/?#].*)?", raw.lower()):
        return f"https://{raw}"

    return None


def _deterministic_tool_route(user_text: str, cfg: dict) -> tuple[str, dict] | None:
    text = (user_text or "").strip()
    if not text:
        return None

    search_target = _strip_prefix(text, _SEARCH_PREFIXES)
    if search_target:
        return "search_web", {"query": search_target}

    visit_target = _strip_prefix(text, _VISIT_PREFIXES)
    if visit_target:
        url = _match_url_target(visit_target)
        if url:
            return "open_url", {"url": url}

    open_target = _strip_prefix(text, _OPEN_PREFIXES)
    if open_target:
        app_name = _match_safe_app(open_target, cfg)
        if app_name:
            return "open_app", {"name": app_name}

        url = _match_url_target(open_target)
        if url:
            return "open_url", {"url": url}

    return None


async def parse_command(client: LLMClient, user_text: str, cfg: dict) -> tuple[str, dict]:
    """Route the user's natural-language command to (tool_name, args).

    Falls back to ``("reply", {"text": "..."})`` on any parse failure.
    """
    from . import tools  # local import to avoid cycles during test collection

    deterministic = _deterministic_tool_route(user_text, cfg)
    if deterministic is not None:
        return deterministic

    messages = [
        {"role": "system", "content": tools.catalog_prompt(cfg)},
        {"role": "user", "content": user_text.strip()},
    ]
    try:
        raw = await client.chat(messages)
    except Exception as e:  # noqa: BLE001
        log.warning("parse_command: chat failed: %s", e)
        return "reply", {"text": "I couldn't reach the model just now."}

    if not (raw or "").strip():
        log.info("parse_command: empty model response")
        return "reply", {"text": "I couldn't reach the model just now."}

    patch, err = _parse_json_loose(raw)
    if patch is None or not isinstance(patch, dict):
        log.info("parse_command: parse failed (%s); raw=%r", err, (raw or "")[:200])
        return "reply", {"text": (raw or "I didn't follow that.").strip()[:400]}

    tool = str(patch.get("tool") or "").strip()
    args = patch.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    if not tool:
        return "reply", {"text": (raw or "I didn't follow that.").strip()[:400]}
    return tool, args


async def run(
    client: LLMClient,
    exec_bus: ExecutiveBus,
    policy_store: PolicyStore,
    command_queue: asyncio.Queue[str],
    snapshot: Snapshot,
    cfg: dict | None = None,
) -> None:
    """Consume user commands forever. Never exits on its own.

    When ``cfg`` is provided and tools are enabled, commands are routed
    through the tool-use layer (``open_url`` / ``open_app`` / …). When no
    ``cfg`` is supplied (legacy call sites, tests) the loop falls back to
    the policy-patch parser.
    """
    from . import tools as tools_mod

    use_tools = bool(cfg is not None)

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
            await publish_event(exec_bus, snapshot, kind="prompt", text=user_text)
            await publish_event(exec_bus, snapshot, kind="status", text="thinking")

            if use_tools:
                tool, args = await parse_command(client, user_text, cfg or {})
                ok, message = await tools_mod.execute(tool, args, cfg or {})
                kind = "tool_ok" if ok else "tool_err"
                await publish_event(
                    exec_bus,
                    snapshot,
                    kind=kind,
                    text=message,
                    data={"tool": tool, "args": args},
                )
                await publish_event(exec_bus, snapshot, kind="status", text="idle")
                continue

            # Legacy policy-patch path (kept so existing unit tests still pass).
            current = policy_store.get()
            new_policy = await parse_intent(client, user_text, current)

            if new_policy is None:
                await publish_event(
                    exec_bus,
                    snapshot,
                    kind="error",
                    text="parse_failed: null policy",
                )
                new_policy = current

            try:
                await policy_store.set(new_policy)
                snapshot.policy = new_policy
            except Exception as e:  # noqa: BLE001
                await publish_event(
                    exec_bus,
                    snapshot,
                    kind="error",
                    text=f"parse_failed: policy store set: {e}",
                )

            await publish_event(
                exec_bus,
                snapshot,
                kind="policy",
                data=new_policy.to_dict(),
            )
            await publish_event(
                exec_bus,
                snapshot,
                kind="explain",
                text=describe_policy_change(current, new_policy),
            )
            await publish_event(exec_bus, snapshot, kind="status", text="idle")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("run: unhandled error: %s", e)
            try:
                await publish_event(
                    exec_bus,
                    snapshot,
                    kind="error",
                    text=f"executive_error: {e}",
                )
            except Exception:  # noqa: BLE001
                pass
            # Keep looping.
            continue
