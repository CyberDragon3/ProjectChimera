"""Tests for the thermal sensor backend + sensor loop."""

from __future__ import annotations

import contextlib
from unittest.mock import patch  # noqa: F401 — reserved per plan

import pytest

from chimera.sensors.thermal import LhmThermalBackend, make_default_thermal_backend  # noqa: F401


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
