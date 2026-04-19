"""Tests for the thermal sensor backend + sensor loop."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch  # noqa: F401 — reserved per plan

import pytest

from chimera.bus import Bus
from chimera.sensors.thermal import (  # noqa: F401
    LhmThermalBackend,
    ThermalSensor,
    make_default_thermal_backend,
)
from chimera.store import RingBuffer


class _FakeWmiError(Exception):
    """Stand-in for wmi.x_wmi (we don't import wmi on non-Windows CI)."""


def test_lhm_backend_logs_missing_namespace(monkeypatch, capsys):
    """When the LHM WMI namespace isn't registered, backend init logs a remediation hint.

    Deviation from plan: asserts against stdout via ``capsys`` rather than
    ``caplog`` because this project's structlog setup writes directly to
    stdout (``make_filtering_bound_logger`` with the default ``PrintLogger``
    factory) and does not route through stdlib ``logging`` — so ``caplog``
    never sees these records. The behavioural check is identical.
    """
    import sys
    fake_wmi = type(sys)("wmi")
    def _raise(*args, **kwargs):
        err = _FakeWmiError("Invalid namespace ")
        err.com_error = ("SWbemLocator", None, (0, None, None, None, 0, -2147217394))
        raise err
    fake_wmi.WMI = _raise  # type: ignore[attr-defined]
    fake_wmi.x_wmi = _FakeWmiError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wmi", fake_wmi)
    monkeypatch.setattr(sys, "platform", "win32")

    backend = make_default_thermal_backend()
    out = capsys.readouterr().out

    assert backend.read_celsius() is None
    assert "lhm_service_missing" in out


class _FlakyBackend:
    """Returns None for the first 2 polls, then real readings."""
    def __init__(self) -> None:
        self.calls = 0
    def read_celsius(self) -> float | None:
        self.calls += 1
        if self.calls <= 2:
            return None
        return 55.0 + self.calls


@pytest.mark.asyncio
async def test_thermal_sensor_logs_online_on_first_reading(capsys):
    """Sensor logs ``sensor.thermal.online`` exactly once on first good sample.

    Deviation from plan: asserts against stdout via ``capsys`` rather than
    ``caplog`` for the same reason as the namespace-missing test — this
    project's structlog setup writes directly to stdout, not through stdlib
    ``logging``. Also temporarily elevates log level to INFO because the
    session-scoped ``_configure_logging`` fixture pins it at WARNING.
    """
    import structlog

    from chimera import logging as log_cfg
    structlog.reset_defaults()
    log_cfg.configure(level="INFO", fmt="console")
    # Thermal module grabbed its logger at import-time when cache said WARNING;
    # bust it so the reconfigure actually takes effect on the existing binding.
    import chimera.sensors.thermal as _thermal_mod
    _thermal_mod.log = structlog.get_logger(_thermal_mod.__name__)

    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    sensor = ThermalSensor(bus, _FlakyBackend(), buf, interval_ms=10)

    task = asyncio.create_task(sensor.run())
    try:
        await asyncio.sleep(0.08)   # ~5+ polls
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    out = capsys.readouterr().out
    online_count = out.count("sensor.thermal.online")
    assert online_count == 1, (
        f"must log online exactly once on first good sample (saw {online_count})"
    )
