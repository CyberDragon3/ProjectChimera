"""LLM invocation gate + circuit breaker.

Pattern: the frontal lobe only fires when either (a) the user asks, or (b) a
lower-tier module signals a conflict. The gate also enforces a hard minimum
interval between calls and a daily cap so a broken reflex cannot burn tokens.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

# Reject anything that looks like a Windows path or file URL — Briefs are
# explicitly metric summaries, not user content.
_PATHISH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\|^file:|/[^/]+/[^/]+/")

log = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class Brief:
    """A pre-digested summary the gate hands to an advisor.

    NOTE: carries NO window titles or file paths by default. The advisor sees
    only aggregated counts and events.
    """

    reason: str
    stats: dict[str, float | int | str]


class Advisor(Protocol):
    async def advise(self, brief: Brief) -> str: ...


class Clock(Protocol):
    def now(self) -> float: ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


class LLMGate:
    def __init__(
        self,
        advisor: Advisor,
        min_interval_seconds: float = 30.0,
        max_daily_calls: int = 500,
        clock: Clock | None = None,
    ) -> None:
        self._advisor = advisor
        self._min_interval = min_interval_seconds
        self._max_daily = max_daily_calls
        self._clock = clock or MonotonicClock()
        self._last_call: float | None = None
        self._calls_today: int = 0
        self._daily_window_start: float = self._clock.now()

    def _reserve(self) -> tuple[bool, str | None]:
        """Atomically check budget AND consume the slot. Single-thread loop, but
        two concurrent ``invoke()`` coroutines could both pass a separate
        ``_allowed()`` check and exceed the cap. Doing the increment together
        with the check closes that race.
        """
        now = self._clock.now()
        if now - self._daily_window_start >= 86_400:
            self._daily_window_start = now
            self._calls_today = 0
        if self._calls_today >= self._max_daily:
            return False, "daily_cap"
        if self._last_call is not None and now - self._last_call < self._min_interval:
            return False, "rate_limit"
        self._last_call = now
        self._calls_today += 1
        return True, None

    @staticmethod
    def _scrub_stats(
        stats: dict[str, float | int | str],
    ) -> dict[str, float | int | str]:
        """Reject string values that look like file paths/URLs/window titles.

        Brief.stats is typed to allow strings (e.g. severity labels) but the
        invariant is that no user content reaches the advisor. Strings that
        look pathy or that are unusually long are dropped here.
        """
        clean: dict[str, float | int | str] = {}
        for k, v in stats.items():
            if isinstance(v, str):
                if len(v) > 64 or _PATHISH.search(v):
                    log.warning("llm.gate.stat_dropped", key=k)
                    continue
            clean[k] = v
        return clean

    async def invoke(self, brief: Brief) -> str | None:
        ok, why = self._reserve()
        if not ok:
            log.info("llm.gate.blocked", reason=why, brief=brief.reason)
            return None
        scrubbed = Brief(reason=brief.reason, stats=self._scrub_stats(brief.stats))
        log.info("llm.gate.invoke", reason=brief.reason, calls_today=self._calls_today)
        try:
            return await self._advisor.advise(scrubbed)
        except Exception as e:
            log.exception("llm.gate.advisor_failed", error=str(e))
            return None


def digest_factory(
    source: Callable[[], dict[str, float | int | str]],
) -> Callable[[str], Awaitable[Brief]]:
    """Build an async digester that produces a Brief for a given reason."""

    async def _digest(reason: str) -> Brief:
        return Brief(reason=reason, stats=source())

    return _digest
