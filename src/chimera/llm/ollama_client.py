"""Ollama local advisor — phi3:mini by default."""

from __future__ import annotations

import structlog

from chimera.llm.gate import Brief

log = structlog.get_logger(__name__)


class OllamaNarrator:
    def __init__(self, model: str = "phi3:mini") -> None:
        self._model = model

    async def advise(self, brief: Brief) -> str:
        try:
            import ollama  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover — ollama is an optional extra
            log.warning("llm.ollama.missing")
            return ""

        prompt = self._prompt(brief)
        # ollama python client is sync; wrap in run_in_executor if latency matters.
        import asyncio

        def _call() -> str:
            resp = ollama.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
            )
            return str(resp.get("message", {}).get("content", "")).strip()

        return await asyncio.to_thread(_call)

    @staticmethod
    def _prompt(brief: Brief) -> str:
        return (
            "You are Chimera, a laconic digital autonomic nervous system. "
            "Summarize the following system state in one short sentence. "
            "No preamble. Do not echo the numbers.\n\n"
            f"Reason: {brief.reason}\n"
            f"Stats: {brief.stats}\n"
        )
