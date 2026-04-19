"""Tests for the LLM gate (circuit breaker + deterministic advisor)."""

from __future__ import annotations

from dataclasses import dataclass, field

from chimera.llm.gate import Brief, LLMGate


@dataclass
class FakeClock:
    t: float = 0.0

    def now(self) -> float:
        return self.t


@dataclass
class CountingAdvisor:
    calls: list[Brief] = field(default_factory=list)
    reply: str = "ok."

    async def advise(self, brief: Brief) -> str:
        self.calls.append(brief)
        return self.reply


async def test_gate_allows_first_call() -> None:
    clock = FakeClock(t=100.0)
    adv = CountingAdvisor()
    gate = LLMGate(adv, min_interval_seconds=30, clock=clock)
    result = await gate.invoke(Brief(reason="conflict", stats={"pid": 1}))
    assert result == "ok."
    assert len(adv.calls) == 1


async def test_gate_blocks_second_call_within_interval() -> None:
    clock = FakeClock(t=100.0)
    adv = CountingAdvisor()
    gate = LLMGate(adv, min_interval_seconds=30, clock=clock)
    await gate.invoke(Brief(reason="a", stats={}))
    clock.t += 10.0
    result = await gate.invoke(Brief(reason="b", stats={}))
    assert result is None
    assert len(adv.calls) == 1


async def test_gate_allows_after_interval_passes() -> None:
    clock = FakeClock(t=0.0)
    adv = CountingAdvisor()
    gate = LLMGate(adv, min_interval_seconds=30, clock=clock)
    await gate.invoke(Brief(reason="a", stats={}))
    clock.t = 31.0
    r = await gate.invoke(Brief(reason="b", stats={}))
    assert r == "ok."
    assert len(adv.calls) == 2


async def test_daily_cap_trips() -> None:
    clock = FakeClock(t=0.0)
    adv = CountingAdvisor()
    gate = LLMGate(adv, min_interval_seconds=0, max_daily_calls=2, clock=clock)
    await gate.invoke(Brief(reason="1", stats={}))
    clock.t += 1
    await gate.invoke(Brief(reason="2", stats={}))
    clock.t += 1
    r = await gate.invoke(Brief(reason="3", stats={}))
    assert r is None
    assert len(adv.calls) == 2


async def test_daily_cap_resets_after_24h() -> None:
    clock = FakeClock(t=0.0)
    adv = CountingAdvisor()
    gate = LLMGate(adv, min_interval_seconds=0, max_daily_calls=1, clock=clock)
    await gate.invoke(Brief(reason="1", stats={}))
    clock.t = 86_400 + 10
    r = await gate.invoke(Brief(reason="2", stats={}))
    assert r == "ok."
