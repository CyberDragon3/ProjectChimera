"""Tests for the ring-buffer telemetry store."""

from __future__ import annotations

import time

from chimera.store import RingBuffer


def test_append_and_latest() -> None:
    rb = RingBuffer(max_seconds=60)
    rb.append(10.0)
    rb.append(20.0)
    latest = rb.latest()
    assert latest is not None
    assert latest.v == 20.0
    assert len(rb) == 2


def test_eviction_by_age() -> None:
    rb = RingBuffer(max_seconds=0.05)
    rb.append(1.0)
    time.sleep(0.1)
    rb.append(2.0)
    # Writing a fresh sample should evict the stale one.
    assert len(rb) == 1
    latest = rb.latest()
    assert latest is not None and latest.v == 2.0


def test_slope_with_linear_data() -> None:
    rb = RingBuffer(max_seconds=60)
    t0 = time.monotonic()
    for i in range(10):
        rb.append(float(i) * 2.0, ts=t0 + float(i))
    # values increase by 2 per second => slope ~2.0
    slope = rb.slope(seconds=60)
    assert abs(slope - 2.0) < 1e-6


def test_slope_empty_or_single_is_zero() -> None:
    rb = RingBuffer(max_seconds=60)
    assert rb.slope(60) == 0.0
    rb.append(5.0)
    assert rb.slope(60) == 0.0


def test_window_filters_by_age() -> None:
    rb = RingBuffer(max_seconds=60)
    t0 = time.monotonic()
    rb.append(1.0, ts=t0 - 120)  # too old, will be evicted on next append
    rb.append(2.0, ts=t0)
    # Older sample is evicted because it's older than max_seconds.
    assert len(rb.window(60)) == 1


def test_ring_buffer_last_n() -> None:
    buf = RingBuffer(max_seconds=60)
    for c in (1.0, 2.0, 3.0, 4.0):
        buf.append(c)
    assert buf.last_n(2) == [3.0, 4.0]
    assert buf.last_n(10) == [1.0, 2.0, 3.0, 4.0]
