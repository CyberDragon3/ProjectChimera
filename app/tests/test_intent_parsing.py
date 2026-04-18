"""Tests for tier1 intent parsing. No network — chat is mocked."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.contracts import BioPolicy
from app.tier1_executive import OllamaClient, parse_intent


pytestmark = pytest.mark.asyncio


def _make_client(chat_stub) -> OllamaClient:
    """Build a client and monkey-patch its .chat bound method."""
    c = OllamaClient(host="http://localhost:11434", model="qwen2.5:0.5b",
                     timeout_s=5.0, temperature=0.1)

    async def _chat(messages: list[dict]) -> str:
        res = chat_stub(messages)
        if hasattr(res, "__await__"):
            return await res  # type: ignore[misc]
        return res

    c.chat = _chat  # type: ignore[assignment]
    return c


def _baseline_policy() -> BioPolicy:
    return BioPolicy.from_dict({
        "fly": {"sensitivity": 0.5, "looming_threshold": 0.35},
        "worm": {"cpu_pain_threshold": 0.85, "ram_pain_threshold": 0.90,
                 "poke_derivative": 0.25, "dwell_ms": 800},
        "mouse": {"track_target_xy": None, "error_threshold": 120.0,
                  "consecutive_frames": 3},
    })


async def test_wellformed_json_merges():
    def stub(_msgs: list[dict]) -> str:
        return '{"worm": {"cpu_pain_threshold": 0.70}, "fly": {"sensitivity": 0.8}}'

    client = _make_client(stub)
    current = _baseline_policy()
    new_policy = await parse_intent(client, "tighten worm CPU to 70% and bump fly sensitivity", current)

    assert new_policy.worm.cpu_pain_threshold == pytest.approx(0.70)
    assert new_policy.fly.sensitivity == pytest.approx(0.8)
    # Unchanged fields preserved by merge.
    assert new_policy.worm.ram_pain_threshold == pytest.approx(0.90)
    assert new_policy.worm.dwell_ms == 800
    assert new_policy.fly.looming_threshold == pytest.approx(0.35)
    assert new_policy.mouse.error_threshold == pytest.approx(120.0)
    await client.aclose()


async def test_json_fenced_still_parses():
    def stub(_msgs: list[dict]) -> str:
        return (
            "Sure, here's the patch:\n"
            "```json\n"
            '{"fly": {"looming_threshold": 0.25}}\n'
            "```\n"
            "Done."
        )

    client = _make_client(stub)
    current = _baseline_policy()
    new_policy = await parse_intent(client, "drop looming threshold to 0.25", current)

    assert new_policy.fly.looming_threshold == pytest.approx(0.25)
    assert new_policy.fly.sensitivity == pytest.approx(0.5)  # unchanged
    await client.aclose()


async def test_garbage_response_falls_back():
    def stub(_msgs: list[dict]) -> str:
        return "I'm sorry Dave, I can't do that."

    client = _make_client(stub)
    current = _baseline_policy()
    new_policy = await parse_intent(client, "do weird things", current)

    assert new_policy.to_dict() == current.to_dict()
    await client.aclose()


async def test_empty_response_falls_back():
    def stub(_msgs: list[dict]) -> str:
        return ""

    client = _make_client(stub)
    current = _baseline_policy()
    new_policy = await parse_intent(client, "nothing", current)

    assert new_policy.to_dict() == current.to_dict()
    await client.aclose()


async def test_partial_json_only_updates_named_field():
    def stub(_msgs: list[dict]) -> str:
        return '{"worm": {"cpu_pain_threshold": 0.65}}'

    client = _make_client(stub)
    current = _baseline_policy()
    new_policy = await parse_intent(client, "tighten worm CPU pain to 65%", current)

    # Changed:
    assert new_policy.worm.cpu_pain_threshold == pytest.approx(0.65)

    # Everything else identical to input.
    cur_d = current.to_dict()
    new_d = new_policy.to_dict()
    assert new_d["fly"] == cur_d["fly"]
    assert new_d["mouse"] == cur_d["mouse"]
    # Worm siblings preserved.
    assert new_d["worm"]["ram_pain_threshold"] == cur_d["worm"]["ram_pain_threshold"]
    assert new_d["worm"]["poke_derivative"] == cur_d["worm"]["poke_derivative"]
    assert new_d["worm"]["dwell_ms"] == cur_d["worm"]["dwell_ms"]
    await client.aclose()


async def test_chat_raises_connect_error_returns_current_policy():
    def stub(_msgs: list[dict]) -> str:
        raise httpx.ConnectError("connection refused")

    client = _make_client(stub)
    current = _baseline_policy()

    # Must NOT propagate the exception.
    new_policy = await parse_intent(client, "tighten worm CPU", current)
    assert new_policy.to_dict() == current.to_dict()
    await client.aclose()
