"""Anthropic Claude Haiku 4.5 advisor with prompt caching."""

from __future__ import annotations

import os
from collections.abc import Iterable

import structlog

from chimera.llm.gate import Brief

log = structlog.get_logger(__name__)


SYSTEM_GUIDE = (
    "You are Chimera, a biologically-inspired autonomic nervous system for a "
    "Windows workstation. You speak in one or two short sentences. Explain what "
    "the system just sensed and recommend an action the user might take. You "
    "receive only pre-digested metric summaries — never raw window titles, "
    "file paths, or user content."
)


class ClaudeAdvisor:
    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        protected_processes: Iterable[str] = (),
        api_key_env: str = "ANTHROPIC_API_KEY",
    ) -> None:
        self._model = model
        self._api_key_env = api_key_env
        # Cache the protected-species roster as a stable cache prefix so Anthropic
        # prompt caching reuses the key across calls.
        joined = ", ".join(sorted(set(protected_processes)))
        self._protected_summary = f"Protected processes the system may never kill: {joined}."

    async def advise(self, brief: Brief) -> str:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            log.warning("llm.claude.no_api_key", env=self._api_key_env)
            return ""

        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover
            log.warning("llm.claude.missing")
            return ""

        client = anthropic.AsyncAnthropic(api_key=api_key)
        try:
            msg = await client.messages.create(
                model=self._model,
                max_tokens=160,
                system=[
                    {"type": "text", "text": SYSTEM_GUIDE},
                    {
                        "type": "text",
                        "text": self._protected_summary,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Reason: {brief.reason}\nStats: {brief.stats}\n\n"
                            "Give me one or two short sentences."
                        ),
                    }
                ],
            )
        except Exception as e:  # pragma: no cover — network path
            log.exception("llm.claude.error", error=str(e))
            return ""

        # Extract the first text block.
        for block in msg.content:
            if getattr(block, "type", "") == "text":
                return str(getattr(block, "text", "")).strip()
        return ""
