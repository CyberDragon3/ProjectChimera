"""Tests for the async pub/sub bus."""

from __future__ import annotations

import asyncio

import pytest

from chimera.bus import Bus, Event


async def test_publish_and_receive_exact_topic() -> None:
    bus = Bus()
    q = bus.subscribe("cpu.spike")
    bus.publish(Event(topic="cpu.spike", payload={"pid": 1, "pct": 99.0}))
    ev = await asyncio.wait_for(q.get(), timeout=0.1)
    assert ev.topic == "cpu.spike"
    assert ev.payload["pid"] == 1


async def test_prefix_subscription_receives_nested() -> None:
    bus = Bus()
    q = bus.subscribe("cpu")
    bus.publish(Event(topic="cpu.spike", payload={}))
    bus.publish(Event(topic="cpu.idle", payload={}))
    bus.publish(Event(topic="thermal.rising", payload={}))
    first = await asyncio.wait_for(q.get(), timeout=0.1)
    second = await asyncio.wait_for(q.get(), timeout=0.1)
    assert {first.topic, second.topic} == {"cpu.spike", "cpu.idle"}
    assert q.empty()


async def test_unrelated_topic_is_ignored() -> None:
    bus = Bus()
    q = bus.subscribe("thermal")
    bus.publish(Event(topic="cpu.spike", payload={}))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.05)


async def test_overflow_drops_and_counts() -> None:
    bus = Bus(queue_maxsize=2)
    bus.subscribe("x")  # subscriber never consumes
    for i in range(5):
        bus.publish(Event(topic="x", payload={"i": i}))
    assert bus.dropped == 3


async def test_prefix_does_not_match_partial_segment() -> None:
    bus = Bus()
    q = bus.subscribe("cpu")
    bus.publish(Event(topic="cpuinfo", payload={}))  # not a cpu.* event
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.05)
