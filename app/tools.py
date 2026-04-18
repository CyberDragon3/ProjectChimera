"""Real computer-control tools.

Each tool is an ``async`` function that returns ``(ok: bool, message: str)``.
The LLM router (tier1 executive) picks one per user command and this module
executes it. Destructive tools are gated by a per-tool allow flag in
``cfg.tools`` — the UI turns them on explicitly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import sys
import urllib.parse
import webbrowser
from typing import Any

log = logging.getLogger("chimera.tools")


# ---------------------------------------------------------------------------
# Tool catalog (fed to the LLM router in the system prompt)
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "open_url",
        "desc": "Open a URL in the default browser. Always include the full scheme (https://).",
        "params": {"url": "string, full https URL"},
        "example": {"tool": "open_url", "args": {"url": "https://github.com/trending"}},
    },
    {
        "name": "open_app",
        "desc": "Launch a local application by friendly name from the allowed app list.",
        "params": {"name": "friendly app name — e.g. chrome, notepad, calculator, spotify, vscode, terminal"},
        "example": {"tool": "open_app", "args": {"name": "chrome"}},
    },
    {
        "name": "search_web",
        "desc": "Run a Google search in the default browser.",
        "params": {"query": "search terms"},
        "example": {"tool": "search_web", "args": {"query": "best pho in san jose"}},
    },
    {
        "name": "type_text",
        "desc": "Type text into the currently focused window (requires explicit permission).",
        "params": {"text": "literal text to type"},
        "example": {"tool": "type_text", "args": {"text": "hello world"}},
    },
    {
        "name": "run_shell",
        "desc": "Run a shell command (requires explicit permission, heavily restricted).",
        "params": {"cmd": "shell command string"},
        "example": {"tool": "run_shell", "args": {"cmd": "dir"}},
    },
    {
        "name": "reply",
        "desc": "Just talk back — when the user is chatting or asking a factual question that doesn't need a tool.",
        "params": {"text": "short reply (<60 words)"},
        "example": {"tool": "reply", "args": {"text": "Booted. Standing by."}},
    },
]


def catalog_prompt(cfg: dict[str, Any]) -> str:
    """Render the tool catalog as a system prompt fragment, respecting the
    per-tool allow flags so the model doesn't pick disabled tools."""
    tools_cfg = cfg.get("tools") or {}
    safe_apps = list((tools_cfg.get("safe_apps") or {}).keys())

    lines: list[str] = [
        "You are Jarvis, an executive assistant running on the user's Windows machine.",
        "You have a small, explicit set of tools. Pick ONE tool per user command and output ONLY a JSON object:",
        '{ "tool": "<name>", "args": { ... } }',
        "No prose, no markdown fences, no commentary. Fill args with the minimum needed.",
        "",
        "Available tools:",
    ]
    for spec in TOOL_SPECS:
        name = spec["name"]
        allowed = True
        if name in ("type_text", "run_shell"):
            allowed = bool(tools_cfg.get(name, False))
        elif name in ("open_url", "open_app", "search_web"):
            allowed = bool(tools_cfg.get(name, True))
        if not allowed:
            continue
        lines.append(f'- {name}: {spec["desc"]}')
        lines.append(f"  params: {spec['params']}")
        lines.append(f"  example: {spec['example']}")

    if safe_apps:
        lines.append("")
        lines.append(f"Allowed open_app names: {', '.join(safe_apps)}")

    lines.extend([
        "",
        "Rules:",
        "- If the user asks to open, launch, start, or bring up any website, use open_url with the full https:// URL.",
        "- 'open youtube' or 'open twitter' → open_url with the correct homepage URL.",
        "- 'open chrome' → open_app with name chrome.",
        "- 'search X' or 'google X' → search_web.",
        "- Chat / questions with no action → reply with a concise answer.",
        "- Never invent tools that aren't listed. Never emit multiple tool calls.",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _run_blocking(func, *args, **kwargs):
    """Run a blocking call in a thread so we don't freeze the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


async def open_url(url: str) -> tuple[bool, str]:
    if not url or "://" not in url:
        return False, f"open_url: need a full URL with scheme, got {url!r}"
    try:
        await _run_blocking(webbrowser.open, url, 2, True)
        return True, f"Opened {url}"
    except Exception as exc:  # noqa: BLE001
        return False, f"open_url failed: {exc}"


async def search_web(query: str) -> tuple[bool, str]:
    if not query or not query.strip():
        return False, "search_web: empty query"
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query.strip())
    ok, msg = await open_url(url)
    return ok, (f"Searched: {query}" if ok else msg)


async def open_app(name: str, safe_apps: dict[str, str]) -> tuple[bool, str]:
    if not name:
        return False, "open_app: missing name"
    key = name.strip().lower()
    cmd = safe_apps.get(key)
    if cmd is None:
        return False, (
            f"open_app: '{name}' is not in the allowed app list. "
            f"Allowed: {', '.join(safe_apps.keys()) or '(none configured)'}"
        )

    def _launch() -> None:
        if sys.platform.startswith("win"):
            # start-via-cmd lets us use PATHEXT / App Paths resolution.
            subprocess.Popen(
                ["cmd", "/c", "start", "", cmd],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True,
            )
        else:
            subprocess.Popen(shlex.split(cmd), close_fds=True)

    try:
        await _run_blocking(_launch)
        return True, f"Launched {name}"
    except FileNotFoundError:
        return False, f"open_app: '{cmd}' not found on PATH"
    except Exception as exc:  # noqa: BLE001
        return False, f"open_app failed: {exc}"


async def type_text(text: str, allowed: bool) -> tuple[bool, str]:
    if not allowed:
        return False, "type_text is disabled. Enable it from Settings first."
    if not text:
        return False, "type_text: empty"
    try:
        from pynput.keyboard import Controller  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return False, f"type_text: pynput unavailable ({exc})"

    def _type() -> None:
        controller = Controller()
        controller.type(text)

    try:
        await _run_blocking(_type)
        return True, f"Typed {len(text)} chars"
    except Exception as exc:  # noqa: BLE001
        return False, f"type_text failed: {exc}"


_SHELL_DENY = (
    "format", "del ", "rmdir", "rm -rf", "shutdown", "reg delete",
    "diskpart", "fdisk", "bcdedit", "takeown",
)


async def run_shell(cmd: str, allowed: bool) -> tuple[bool, str]:
    if not allowed:
        return False, "run_shell is disabled. Enable it from Settings first."
    if not cmd or not cmd.strip():
        return False, "run_shell: empty"
    lowered = cmd.lower()
    for bad in _SHELL_DENY:
        if bad in lowered:
            return False, f"run_shell: blocked a destructive token ({bad!r})"

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15.0,
        )

    try:
        result = await _run_blocking(_run)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode != 0 and err:
            return False, f"run_shell: exit {result.returncode}: {err[:400]}"
        summary = out[:600] or f"(exit {result.returncode})"
        return True, summary
    except subprocess.TimeoutExpired:
        return False, "run_shell: timed out after 15s"
    except Exception as exc:  # noqa: BLE001
        return False, f"run_shell failed: {exc}"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(tool: str, args: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str]:
    """Execute ``tool`` with ``args``, consulting the allow-list in ``cfg``.

    Returns ``(ok, message)``. Never raises.
    """
    tools_cfg = cfg.get("tools") or {}
    safe_apps = tools_cfg.get("safe_apps") or {}

    if tool == "open_url":
        if not tools_cfg.get("open_url", True):
            return False, "open_url is disabled."
        return await open_url(str((args or {}).get("url", "")))

    if tool == "open_app":
        if not tools_cfg.get("open_app", True):
            return False, "open_app is disabled."
        return await open_app(str((args or {}).get("name", "")), dict(safe_apps))

    if tool == "search_web":
        if not tools_cfg.get("search_web", True):
            return False, "search_web is disabled."
        return await search_web(str((args or {}).get("query", "")))

    if tool == "type_text":
        return await type_text(
            str((args or {}).get("text", "")),
            bool(tools_cfg.get("type_text", False)),
        )

    if tool == "run_shell":
        return await run_shell(
            str((args or {}).get("cmd", "")),
            bool(tools_cfg.get("run_shell", False)),
        )

    if tool == "reply":
        text = str((args or {}).get("text", "")).strip()
        return True, text or "(no reply)"

    return False, f"Unknown tool: {tool!r}"
