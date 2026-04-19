# Biological Veto Hierarchy v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Zebrafish > Mouse > Worm veto hierarchy end-to-end, fix `thermal: n/a`, add Lysosome scavenger, and elevate the daemon — per spec `docs/superpowers/specs/2026-04-19-biological-veto-hierarchy-design.md`.

**Architecture:** Add three new bus topics (`thermal.critical`, `cortex.protect_foreground`, `lysosome.sweep`). Zebrafish publishes a hard-floor critical signal with hysteresis. Mouse publishes a global foreground-protection flag keyed by PID. Worm caches both and consults them in a fixed precedence order at each throttle decision. Lysosome runs a three-phase sweep on `idle.enter`. Whole daemon runs admin via Task Scheduler "Highest" run level.

**Tech Stack:** Python 3.11, asyncio, psutil, pydantic v2, pywin32 + wmi (Windows), ctypes (psapi, kernel32), pytest + pytest-asyncio, structlog, ruff, mypy strict. Inno Setup 6 + PyInstaller for packaging.

**Parallelism hints for fleet dispatch:**
- Phase 1 ‖ Phase 2 ‖ Phase 4 ‖ Phase 8 (all independent)
- Phase 3 depends on Phase 2
- Phase 5 depends on Phase 3 + Phase 4
- Phase 6 depends on Phase 2
- Phase 7 depends on Phase 6

---

## File Structure

**New files**
- `src/chimera/reflexes/lysosome.py` — Lysosome scavenger reflex.
- `tests/reflexes/test_worm_veto.py` — Worm precedence tests.
- `tests/reflexes/test_lysosome.py` — Lysosome sweep tests.
- `tests/reflexes/test_mouse_protect.py` — Mouse protect_foreground tests.
- `tests/sensors/test_thermal_critical.py` — Zebrafish critical state machine tests.
- `docs/UPGRADING.md` — elevation migration note.

**Modified files**
- `src/chimera/sensors/thermal.py` — LHM diagnostics + retry wrapper.
- `src/chimera/reflexes/zebrafish.py` — add critical state machine.
- `src/chimera/reflexes/mouse.py` — add `cortex.protect_foreground` publisher.
- `src/chimera/reflexes/worm.py` — subscribe to critical + protect_foreground; apply precedence.
- `src/chimera/reflexes/fly.py` — confirm `idle.enter`/`idle.exit` are emitted as-is (no change expected; add `system.deep_breath` on `idle.enter` for Lysosome).
- `src/chimera/config.py` — extend `Thresholds`, add `LysosomeCfg`, add to `Settings`.
- `src/chimera/daemon.py` — spawn Lysosome task; wire new config.
- `src/chimera/store.py` — add `last_n(n)` helper on `RingBuffer` if not present (verified in Task 2).
- `config/chimera.toml` — new keys.
- `tests/test_safety_audit.py` — allowlist Lysosome; add `test_lysosome_kill_calls_are_gated`.
- `installer/chimera.iss` — `Principal.RunLevel = Highest`.
- `installer/chimera.spec` — `uac_admin=True` on EXE target.
- `scripts/install_task.ps1` — `-RunLevel Highest`.

**Branches/commits:** one commit per numbered task where possible. Stay on `homeostasis` branch (current).

---

## Phase 1 — Thermal Diagnostics & `n/a` Fix

### Task 1: Add LHM-namespace-missing diagnostic

**Files:**
- Modify: `src/chimera/sensors/thermal.py`
- Test: `tests/sensors/test_thermal.py` (extend)

- [ ] **Step 1.1: Write the failing test**

Append to `tests/sensors/test_thermal.py` (create the file if it does not yet exist):

```python
from unittest.mock import patch

import pytest

from chimera.sensors.thermal import LhmThermalBackend, make_default_thermal_backend


class _FakeWmiError(Exception):
    """Stand-in for wmi.x_wmi (we don't import wmi on non-Windows CI)."""


def test_lhm_backend_logs_missing_namespace(monkeypatch, caplog):
    """When the LHM WMI namespace isn't registered, backend init logs a remediation hint."""
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

    with caplog.at_level("WARNING"):
        backend = make_default_thermal_backend()

    assert backend.read_celsius() is None  # null fallback
    assert any("lhm_service_missing" in r.message for r in caplog.records)
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `pytest tests/sensors/test_thermal.py::test_lhm_backend_logs_missing_namespace -v`
Expected: FAIL — no `lhm_service_missing` log event yet.

- [ ] **Step 1.3: Implement the diagnostic in `thermal.py`**

Replace the `make_default_thermal_backend` function in `src/chimera/sensors/thermal.py`:

```python
def make_default_thermal_backend() -> ThermalBackend:
    if sys.platform != "win32":
        return NullThermalBackend()
    try:
        return LhmThermalBackend()
    except Exception as e:
        msg = str(e).lower()
        if "invalid namespace" in msg or "0x8004100e" in msg or "2147217394" in msg:
            log.warning(
                "sensor.thermal.lhm_service_missing",
                error=str(e),
                remediation=(
                    "LibreHardwareMonitor WMI namespace not found. "
                    "Install + run LHM as admin: scripts/install_lhm.ps1"
                ),
            )
        else:
            log.warning("sensor.thermal.unavailable", error=str(e))
    return NullThermalBackend()
```

- [ ] **Step 1.4: Run test**

Run: `pytest tests/sensors/test_thermal.py::test_lhm_backend_logs_missing_namespace -v`
Expected: PASS.

- [ ] **Step 1.5: Run full test file to guard regressions**

Run: `pytest tests/sensors/test_thermal.py -v`
Expected: all PASS.

- [ ] **Step 1.6: Commit**

```bash
rtk git add src/chimera/sensors/thermal.py tests/sensors/test_thermal.py
rtk git commit -m "fix(thermal): emit lhm_service_missing diagnostic when WMI namespace absent"
```

### Task 2: Add online-signal log + retry wrapper

**Files:**
- Modify: `src/chimera/sensors/thermal.py`
- Test: `tests/sensors/test_thermal.py`

- [ ] **Step 2.1: Write the failing test**

Append to `tests/sensors/test_thermal.py`:

```python
import asyncio

from chimera.bus import Bus
from chimera.sensors.thermal import ThermalSensor
from chimera.store import RingBuffer


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
async def test_thermal_sensor_logs_online_on_first_reading(caplog):
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    sensor = ThermalSensor(bus, _FlakyBackend(), buf, interval_ms=10)

    task = asyncio.create_task(sensor.run())
    try:
        with caplog.at_level("INFO"):
            await asyncio.sleep(0.08)   # ~5+ polls
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    online = [r for r in caplog.records if "sensor.thermal.online" in r.message]
    assert len(online) == 1, "must log online exactly once on first good sample"
```

Add `import contextlib` at top of test file.

- [ ] **Step 2.2: Run test to verify it fails**

Run: `pytest tests/sensors/test_thermal.py::test_thermal_sensor_logs_online_on_first_reading -v`
Expected: FAIL — no `sensor.thermal.online` log emitted.

- [ ] **Step 2.3: Implement the online signal**

In `src/chimera/sensors/thermal.py`, modify `ThermalSensor.run`:

```python
async def run(self) -> None:
    log.info("sensor.thermal.start", interval_ms=int(self._interval * 1000))
    online_logged = False
    while True:
        try:
            c = await asyncio.to_thread(self._backend.read_celsius)
            if c is not None:
                if not online_logged:
                    log.info("sensor.thermal.online", celsius=c)
                    online_logged = True
                self._buf.append(c)
                self._bus.publish(
                    Event(topic="thermal.sample", payload={"celsius": c}, ts=time.monotonic())
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("sensor.thermal.iteration_failed", error=str(e))
        await asyncio.sleep(self._interval)
```

- [ ] **Step 2.4: Run tests**

Run: `pytest tests/sensors/test_thermal.py -v`
Expected: all PASS.

- [ ] **Step 2.5: Commit**

```bash
rtk git add src/chimera/sensors/thermal.py tests/sensors/test_thermal.py
rtk git commit -m "feat(thermal): emit sensor.thermal.online on first successful reading"
```

---

## Phase 2 — Config Surface Extension

### Task 3: Extend `Thresholds` and add `LysosomeCfg`

**Files:**
- Modify: `src/chimera/config.py`
- Modify: `config/chimera.toml`
- Test: `tests/test_config.py` (create if absent)

- [ ] **Step 3.1: Write the failing test**

Create `tests/test_config.py` (or append if exists):

```python
from pathlib import Path

import pytest

from chimera.config import Settings, load


def test_settings_has_thermal_critical_defaults():
    s = Settings()
    assert s.thresholds.thermal_critical_c == 95.0
    assert s.thresholds.thermal_critical_clear_c == 90.0
    assert s.thresholds.thermal_critical_samples == 2
    assert s.thresholds.thermal_critical_max_hold_seconds == 300


def test_settings_has_lysosome_defaults():
    s = Settings()
    assert s.lysosome.enabled is True
    assert s.lysosome.sweep_interval_seconds == 600
    assert s.lysosome.targets == ()


def test_lysosome_targets_is_immutable():
    s = Settings(lysosome={"targets": ["foo.exe", "bar.exe"]})
    assert s.lysosome.targets == ("foo.exe", "bar.exe")
    with pytest.raises((TypeError, ValueError)):
        s.lysosome.targets = ("evil.exe",)  # frozen


def test_load_default_toml_parses_new_keys(tmp_path: Path):
    toml = tmp_path / "c.toml"
    toml.write_text(
        "[thresholds]\n"
        "thermal_critical_c = 92.5\n"
        "[lysosome]\n"
        "enabled = false\n"
        "targets = [\"chrome_crashpad_handler.exe\"]\n"
    )
    s = load(toml)
    assert s.thresholds.thermal_critical_c == 92.5
    assert s.lysosome.enabled is False
    assert s.lysosome.targets == ("chrome_crashpad_handler.exe",)
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `Thresholds` has no `thermal_critical_c`; `Settings` has no `lysosome`.

- [ ] **Step 3.3: Extend `Thresholds` and add `LysosomeCfg` in `config.py`**

Modify `src/chimera/config.py`:

```python
class Thresholds(_Frozen):
    cpu_spike_percent: float = 85.0
    cpu_sustained_seconds: float = 1.0
    thermal_slope_c_per_min: float = 2.5
    idle_seconds: int = 300
    reflex_deadline_ms: int = 10
    thermal_critical_c: float = 95.0
    thermal_critical_clear_c: float = 90.0
    thermal_critical_samples: int = 2
    thermal_critical_max_hold_seconds: int = 300


class LysosomeCfg(_Frozen):
    enabled: bool = True
    sweep_interval_seconds: int = 600
    targets: tuple[str, ...] = ()
```

Add to `Settings`:

```python
class Settings(_Frozen):
    protected_species: ProtectedSpeciesCfg = Field(default_factory=ProtectedSpeciesCfg)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    poll: Poll = Field(default_factory=Poll)
    store: Store = Field(default_factory=Store)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    dashboard: Dashboard = Field(default_factory=Dashboard)
    logging: Logging = Field(default_factory=Logging)
    lysosome: LysosomeCfg = Field(default_factory=LysosomeCfg)
```

Append to `config/chimera.toml`:

```toml
# — Zebrafish hard-floor (§5.1 of design spec)
# thermal_critical_c = 95.0
# thermal_critical_clear_c = 90.0
# thermal_critical_samples = 2
# thermal_critical_max_hold_seconds = 300

[lysosome]
enabled = true
sweep_interval_seconds = 600   # don't re-sweep within 10 min
targets = []                    # opt-in exe names for phase-3 kill
```

- [ ] **Step 3.4: Run test**

Run: `pytest tests/test_config.py -v`
Expected: all PASS.

- [ ] **Step 3.5: Verify mypy + ruff**

Run: `mypy` and `ruff check src tests`
Expected: no new errors.

- [ ] **Step 3.6: Commit**

```bash
rtk git add src/chimera/config.py config/chimera.toml tests/test_config.py
rtk git commit -m "feat(config): add thermal_critical_* thresholds and [lysosome] section"
```

---

## Phase 3 — Zebrafish Critical State Machine

### Task 4: Add `thermal.critical` publisher to Zebrafish

**Files:**
- Modify: `src/chimera/reflexes/zebrafish.py`
- Test: `tests/sensors/test_thermal_critical.py` (new)

- [ ] **Step 4.1: Write the failing tests**

Create `tests/sensors/test_thermal_critical.py`:

```python
"""Zebrafish critical state machine — hard-floor temperature veto."""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.zebrafish import ZebrafishReflex
from chimera.store import RingBuffer


async def _collect(bus: Bus, topic: str, duration: float) -> list[Event]:
    q = bus.subscribe(topic)
    events: list[Event] = []
    try:
        async with asyncio.timeout(duration):
            while True:
                events.append(await q.get())
    except TimeoutError:
        pass
    finally:
        bus.unsubscribe(topic, q)
    return events


@pytest.mark.asyncio
async def test_critical_fires_after_n_consecutive_samples_above_threshold():
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    # Push samples: 3 consecutive >= 95 triggers entry (samples=2 needs only 2)
    for c in (96.0, 97.0):
        buf.append(c)

    z = ZebrafishReflex(
        bus, buf,
        slope_c_per_min_threshold=2.5,
        critical_c=95.0, critical_clear_c=90.0, critical_samples=2,
        max_hold_seconds=300,
        interval_ms=10,
    )
    collector = asyncio.create_task(_collect(bus, "thermal.critical", 0.08))
    task = asyncio.create_task(z.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert any(e.payload.get("on") is True for e in events)


@pytest.mark.asyncio
async def test_critical_clears_after_n_consecutive_samples_below_clear():
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    # Start above, then drop well below clear threshold
    for c in (96.0, 97.0, 80.0, 79.0):
        buf.append(c)

    z = ZebrafishReflex(
        bus, buf,
        slope_c_per_min_threshold=2.5,
        critical_c=95.0, critical_clear_c=90.0, critical_samples=2,
        max_hold_seconds=300,
        interval_ms=10,
    )
    # Prime the reflex so it sees the on-transition first, then off.
    z._critical_on = True  # type: ignore[attr-defined]

    collector = asyncio.create_task(_collect(bus, "thermal.critical", 0.08))
    task = asyncio.create_task(z.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert any(e.payload.get("on") is False for e in events)


@pytest.mark.asyncio
async def test_critical_does_not_flap_between_clear_and_critical():
    """Mid-range samples (91°C) should neither fire nor clear."""
    bus = Bus()
    buf = RingBuffer(max_seconds=60)
    for c in (91.0, 91.5, 92.0, 92.5):
        buf.append(c)

    z = ZebrafishReflex(
        bus, buf,
        slope_c_per_min_threshold=2.5,
        critical_c=95.0, critical_clear_c=90.0, critical_samples=2,
        max_hold_seconds=300,
        interval_ms=10,
    )
    collector = asyncio.create_task(_collect(bus, "thermal.critical", 0.08))
    task = asyncio.create_task(z.run())
    try:
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert events == []
```

- [ ] **Step 4.2: Add `last_n` helper to RingBuffer if missing**

First, inspect `src/chimera/store.py`. If there's no method exposing the last N raw samples, add:

```python
def last_n(self, n: int) -> list[float]:
    """Return up to the last n (ts, value) values — most recent last."""
    items = list(self._items)[-n:]
    return [v for _, v in items]
```

Write a quick test in `tests/test_store.py` if needed (append):

```python
def test_ring_buffer_last_n():
    from chimera.store import RingBuffer
    buf = RingBuffer(max_seconds=60)
    for c in (1.0, 2.0, 3.0, 4.0):
        buf.append(c)
    assert buf.last_n(2) == [3.0, 4.0]
    assert buf.last_n(10) == [1.0, 2.0, 3.0, 4.0]
```

- [ ] **Step 4.3: Run critical test to confirm failure**

Run: `pytest tests/sensors/test_thermal_critical.py -v`
Expected: FAIL — constructor does not accept `critical_c`, etc.

- [ ] **Step 4.4: Extend `ZebrafishReflex` with critical state machine**

Replace `src/chimera/reflexes/zebrafish.py` body:

```python
"""Zebrafish governor — thermal metabolism slope + hard-floor critical veto.

Two independent signals:
- ``thermal.rising`` — slope over a 60 s window (existing).
- ``thermal.critical`` — hard-floor absolute threshold with hysteresis (new).
  Consumed by Worm as the supreme-veto override (see design §4.2).
"""

from __future__ import annotations

import asyncio
import time

import structlog

from chimera.bus import Bus, Event
from chimera.store import RingBuffer

log = structlog.get_logger(__name__)


class ZebrafishReflex:
    def __init__(
        self,
        bus: Bus,
        buffer: RingBuffer,
        slope_c_per_min_threshold: float,
        critical_c: float = 95.0,
        critical_clear_c: float = 90.0,
        critical_samples: int = 2,
        max_hold_seconds: int = 300,
        window_seconds: float = 60.0,
        interval_ms: int = 5000,
    ) -> None:
        self._bus = bus
        self._buf = buffer
        self._threshold_per_sec = slope_c_per_min_threshold / 60.0
        self._critical_c = critical_c
        self._critical_clear_c = critical_clear_c
        self._critical_samples = critical_samples
        self._max_hold = max_hold_seconds
        self._window = window_seconds
        self._interval = interval_ms / 1000.0
        self._critical_on = False
        self._critical_entered_at: float | None = None

    def _eval_critical(self) -> None:
        recent = self._buf.last_n(self._critical_samples)
        if len(recent) < self._critical_samples:
            return
        now = time.monotonic()
        if not self._critical_on and all(c >= self._critical_c for c in recent):
            self._critical_on = True
            self._critical_entered_at = now
            self._bus.publish(
                Event(
                    topic="thermal.critical",
                    payload={"on": True, "celsius": recent[-1]},
                    ts=now,
                )
            )
            log.warning("reflex.zebrafish.critical_entered", celsius=recent[-1])
            return
        if self._critical_on:
            if all(c <= self._critical_clear_c for c in recent):
                self._critical_on = False
                self._critical_entered_at = None
                self._bus.publish(
                    Event(
                        topic="thermal.critical",
                        payload={"on": False, "celsius": recent[-1]},
                        ts=now,
                    )
                )
                log.info("reflex.zebrafish.critical_cleared", celsius=recent[-1])
                return
            # stuck-sensor auto-clear
            if (
                self._critical_entered_at is not None
                and now - self._critical_entered_at > self._max_hold
            ):
                self._critical_on = False
                self._critical_entered_at = None
                self._bus.publish(
                    Event(
                        topic="thermal.critical",
                        payload={"on": False, "celsius": recent[-1], "reason": "max_hold_exceeded"},
                        ts=now,
                    )
                )
                log.warning("reflex.zebrafish.critical_suspicious_auto_clear")

    def _eval_slope(self) -> None:
        slope = self._buf.slope(self._window)
        if slope >= self._threshold_per_sec:
            severity = "critical" if slope >= 2 * self._threshold_per_sec else "warn"
            self._bus.publish(
                Event(
                    topic="thermal.rising",
                    payload={
                        "slope_c_per_min": slope * 60,
                        "severity": severity,
                        "window_s": self._window,
                    },
                    ts=time.monotonic(),
                )
            )
            log.info(
                "reflex.zebrafish.rising",
                slope_c_per_min=slope * 60,
                severity=severity,
            )

    async def run(self) -> None:
        log.info(
            "reflex.zebrafish.start",
            threshold_c_per_min=self._threshold_per_sec * 60,
            critical_c=self._critical_c,
            critical_clear_c=self._critical_clear_c,
        )
        while True:
            try:
                self._eval_slope()
                self._eval_critical()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("reflex.zebrafish.iteration_failed", error=str(e))
            await asyncio.sleep(self._interval)
```

- [ ] **Step 4.5: Wire new settings in `daemon.py`**

In `src/chimera/daemon.py`, update the Zebrafish construction inside `_start_reflexes`:

```python
zebrafish = ZebrafishReflex(
    self.bus,
    self.thermal_buf,
    slope_c_per_min_threshold=s.thresholds.thermal_slope_c_per_min,
    critical_c=s.thresholds.thermal_critical_c,
    critical_clear_c=s.thresholds.thermal_critical_clear_c,
    critical_samples=s.thresholds.thermal_critical_samples,
    max_hold_seconds=s.thresholds.thermal_critical_max_hold_seconds,
    interval_ms=s.poll.thermal_interval_ms,
)
```

- [ ] **Step 4.6: Run tests**

Run: `pytest tests/sensors/test_thermal_critical.py tests/test_store.py tests/reflexes/test_zebrafish.py -v`
Expected: all PASS (existing slope test must still pass).

- [ ] **Step 4.7: Commit**

```bash
rtk git add src/chimera/reflexes/zebrafish.py src/chimera/store.py src/chimera/daemon.py tests/sensors/test_thermal_critical.py tests/test_store.py
rtk git commit -m "feat(zebrafish): publish thermal.critical with hysteresis and max-hold auto-clear"
```

---

## Phase 4 — Mouse `protect_foreground` Publisher

### Task 5: Mouse publishes `cortex.protect_foreground`

**Files:**
- Modify: `src/chimera/reflexes/mouse.py`
- Test: `tests/reflexes/test_mouse_protect.py` (new)

- [ ] **Step 5.1: Write the failing tests**

Create `tests/reflexes/test_mouse_protect.py`:

```python
"""Mouse publishes cortex.protect_foreground when foreground PID == CPU-spike PID."""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.mouse import MouseReflex


async def _drain(bus: Bus, topic: str, duration: float) -> list[Event]:
    q = bus.subscribe(topic)
    out: list[Event] = []
    try:
        async with asyncio.timeout(duration):
            while True:
                out.append(await q.get())
    except TimeoutError:
        pass
    finally:
        bus.unsubscribe(topic, q)
    return out


@pytest.mark.asyncio
async def test_spike_on_foreground_publishes_protect_on():
    bus = Bus()
    m = MouseReflex(bus)
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.1))
    task = asyncio.create_task(m.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "chrome.exe", "pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0, "rss_bytes": 100},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    on_events = [e for e in events if e.payload.get("on") is True]
    assert any(e.payload.get("foreground_pid") == 1234 for e in on_events)


@pytest.mark.asyncio
async def test_spike_on_non_foreground_does_not_publish_protect_on():
    bus = Bus()
    m = MouseReflex(bus)
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.1))
    task = asyncio.create_task(m.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "chrome.exe", "pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 9999, "exe": "cc.exe", "cpu_percent": 92.0, "rss_bytes": 100},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert not any(e.payload.get("on") is True for e in events)


@pytest.mark.asyncio
async def test_foreground_change_publishes_protect_off():
    bus = Bus()
    m = MouseReflex(bus)
    collector = asyncio.create_task(_drain(bus, "cortex.protect_foreground", 0.1))
    task = asyncio.create_task(m.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "chrome.exe", "pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="window.foreground",
            payload={"exe": "code.exe", "pid": 5678},
            ts=time.monotonic(),
        ))
        events = await collector
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    off_events = [e for e in events if e.payload.get("on") is False]
    assert len(off_events) >= 2  # one per foreground change
```

- [ ] **Step 5.2: Run test to verify failure**

Run: `pytest tests/reflexes/test_mouse_protect.py -v`
Expected: FAIL — Mouse doesn't publish `cortex.protect_foreground`.

- [ ] **Step 5.3: Extend `MouseReflex`**

Replace `src/chimera/reflexes/mouse.py` body:

```python
"""Mouse reflex — semantic context filter + cortex veto publisher.

Keeps the original ``context.active_window`` enrichment (creator-apps → intentional).
Adds ``cortex.protect_foreground`` veto when a CPU spike is sourced by the
currently foregrounded PID — the BMTKCortex instructs the Worm to stand down
so the user's active work isn't throttled (see design §5.2).
"""

from __future__ import annotations

import asyncio
import time

import structlog

from chimera.bus import Bus, Event

log = structlog.get_logger(__name__)

DEFAULT_CREATOR_APPS: frozenset[str] = frozenset(
    {
        "blender.exe", "premiere.exe", "adobe premiere pro.exe", "aftereffects.exe",
        "davinci resolve.exe", "unreal.exe", "unrealeditor.exe", "unity.exe",
        "ffmpeg.exe", "handbrake.exe", "code.exe", "cursor.exe",
        "pycharm64.exe", "devenv.exe", "obs64.exe", "obs32.exe",
    }
)


class MouseReflex:
    def __init__(self, bus: Bus, creator_apps: frozenset[str] = DEFAULT_CREATOR_APPS) -> None:
        self._bus = bus
        self._creators = frozenset(a.lower() for a in creator_apps)
        self._foreground_pid: int | None = None
        self._foreground_exe: str | None = None

    def _publish_protect(self, on: bool) -> None:
        self._bus.publish(
            Event(
                topic="cortex.protect_foreground",
                payload={"on": on, "foreground_pid": self._foreground_pid},
                ts=time.monotonic(),
            )
        )

    async def _handle_window(self, event: Event) -> None:
        try:
            pid_raw = event.payload.get("pid")
            self._foreground_pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            self._foreground_pid = None
        exe = str(event.payload.get("exe", "")).lower()
        self._foreground_exe = exe or None
        intentional = exe in self._creators
        self._bus.publish(
            Event(
                topic="context.active_window",
                payload={**event.payload, "intentional": intentional},
                ts=time.monotonic(),
            )
        )
        # Any foreground change clears any stale protection; next spike re-asserts.
        self._publish_protect(False)
        log.info(
            "reflex.mouse.classified",
            exe=exe, pid=self._foreground_pid, intentional=intentional,
        )

    async def _handle_spike(self, event: Event) -> None:
        try:
            spike_pid = int(event.payload.get("pid", -1))
        except (TypeError, ValueError):
            return
        if self._foreground_pid is not None and spike_pid == self._foreground_pid:
            self._publish_protect(True)
            log.info(
                "reflex.mouse.protect_on",
                pid=spike_pid, exe=event.payload.get("exe"),
            )

    async def run(self) -> None:
        win_q = self._bus.subscribe("window.foreground")
        spike_q = self._bus.subscribe("cpu.spike")
        log.info("reflex.mouse.start", creator_apps=len(self._creators))
        win_task: asyncio.Task[Event] = asyncio.create_task(win_q.get())
        spike_task: asyncio.Task[Event] = asyncio.create_task(spike_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {win_task, spike_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        event = t.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("reflex.mouse.recv_failed", error=str(e))
                        event = None
                    if t is win_task:
                        win_task = asyncio.create_task(win_q.get())
                        if event is not None:
                            await self._handle_window(event)
                    elif t is spike_task:
                        spike_task = asyncio.create_task(spike_q.get())
                        if event is not None:
                            await self._handle_spike(event)
        finally:
            for pending in (win_task, spike_task):
                if not pending.done():
                    pending.cancel()
            self._bus.unsubscribe("window.foreground", win_q)
            self._bus.unsubscribe("cpu.spike", spike_q)
```

- [ ] **Step 5.4: Run tests**

Run: `pytest tests/reflexes/test_mouse_protect.py tests/reflexes/test_mouse.py -v`
Expected: all PASS. (Existing `context.active_window` behavior preserved.)

- [ ] **Step 5.5: Commit**

```bash
rtk git add src/chimera/reflexes/mouse.py tests/reflexes/test_mouse_protect.py
rtk git commit -m "feat(mouse): publish cortex.protect_foreground on foreground-sourced spikes"
```

---

## Phase 5 — Worm Precedence Consumer

### Task 6: Worm subscribes to veto topics and applies precedence

**Files:**
- Modify: `src/chimera/reflexes/worm.py`
- Test: `tests/reflexes/test_worm_veto.py` (new)

- [ ] **Step 6.1: Write the failing tests**

Create `tests/reflexes/test_worm_veto.py`:

```python
"""Worm veto precedence — thermal.critical > cortex.protect_foreground > default."""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.worm import WormReflex
from chimera.safety import ProtectedSpecies


class _RecordingThrottler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
    def demote(self, pid: int, level: int) -> bool:
        self.calls.append((pid, level))
        return True


def _make() -> tuple[Bus, ProtectedSpecies, _RecordingThrottler, WormReflex]:
    bus = Bus()
    safety = ProtectedSpecies.from_list(["winlogon.exe"])
    throttler = _RecordingThrottler()
    worm = WormReflex(bus, safety, throttler, deadline_ms=50)
    return bus, safety, throttler, worm


@pytest.mark.asyncio
async def test_default_path_demotes():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "hog.exe", "cpu_percent": 90.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert throttler.calls == [(1234, throttler.calls[0][1])]


@pytest.mark.asyncio
async def test_protect_foreground_stands_down():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": True, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert throttler.calls == []  # stood down


@pytest.mark.asyncio
async def test_thermal_critical_overrides_protect_foreground():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": True, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        bus.publish(Event(
            topic="thermal.critical",
            payload={"on": True, "celsius": 97.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(throttler.calls) == 1
    assert throttler.calls[0][0] == 1234  # demoted despite protection


@pytest.mark.asyncio
async def test_protected_species_never_demoted_even_under_critical():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="thermal.critical",
            payload={"on": True, "celsius": 97.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 7, "exe": "winlogon.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert throttler.calls == []  # safety gate denied

@pytest.mark.asyncio
async def test_protect_clears_when_off_event_arrives():
    bus, _, throttler, worm = _make()
    task = asyncio.create_task(worm.run())
    try:
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": True, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        bus.publish(Event(
            topic="cortex.protect_foreground",
            payload={"on": False, "foreground_pid": 1234},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.01)
        bus.publish(Event(
            topic="cpu.spike",
            payload={"pid": 1234, "exe": "chrome.exe", "cpu_percent": 92.0},
            ts=time.monotonic(),
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(throttler.calls) == 1  # protection lifted → normal demote
```

- [ ] **Step 6.2: Run to verify failure**

Run: `pytest tests/reflexes/test_worm_veto.py -v`
Expected: FAIL — Worm doesn't yet honor `thermal.critical` / `cortex.protect_foreground`.

- [ ] **Step 6.3: Rewrite `WormReflex.run` and `_handle`**

Replace `src/chimera/reflexes/worm.py` body. Keep existing `Throttler` + `PsutilThrottler`:

```python
"""Worm reflex — CPU pain response with hierarchical veto gating.

Precedence (design §4.2):
  1. safety.gate  (protected species always wins)
  2. thermal.critical — supreme override, ignores protection
  3. cortex.protect_foreground — Mouse-issued stand-down per-PID
  4. exe-level intentional hint (legacy window.foreground creator-apps)
  5. demote
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

import psutil
import structlog

from chimera.bus import Bus, Event
from chimera.safety import ProtectedSpecies

log = structlog.get_logger(__name__)

try:
    _BELOW_NORMAL = psutil.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]
    _IDLE_PRIO = psutil.IDLE_PRIORITY_CLASS  # type: ignore[attr-defined]
except AttributeError:
    _BELOW_NORMAL = 10
    _IDLE_PRIO = 19


class Throttler(Protocol):
    def demote(self, pid: int, level: int) -> bool: ...


class PsutilThrottler:
    def demote(self, pid: int, level: int) -> bool:
        try:
            p = psutil.Process(pid)
            p.nice(level)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.warning("worm.demote_failed", pid=pid, error=str(e))
            return False


class WormReflex:
    def __init__(
        self,
        bus: Bus,
        safety: ProtectedSpecies,
        throttler: Throttler,
        deadline_ms: int = 10,
    ) -> None:
        self._bus = bus
        self._safety = safety
        self._throttler = throttler
        self._deadline = deadline_ms / 1000.0
        self._intentional: dict[str, float] = {}
        self._thermal_critical = False
        self._protect_on = False
        self._protect_pid: int | None = None

    def mark_intentional(self, exe: str, ttl_seconds: float = 10.0) -> None:
        self._intentional[exe.lower()] = time.monotonic() + ttl_seconds

    def _is_intentional(self, exe: str) -> bool:
        expiry = self._intentional.get(exe.lower())
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            self._intentional.pop(exe.lower(), None)
            return False
        return True

    async def _handle(self, event: Event) -> None:
        exe = str(event.payload.get("exe", ""))
        try:
            pid = int(event.payload.get("pid", -1))
            pct = float(event.payload.get("cpu_percent", 0.0))
        except (TypeError, ValueError):
            log.warning("reflex.worm.bad_payload", payload=event.payload)
            return

        if not self._safety.gate(exe, action="throttle", pid=pid):
            return

        if self._thermal_critical:
            log.warning(
                "reflex.worm.critical_override",
                pid=pid, exe=exe, cpu_percent=pct,
            )
        else:
            if self._protect_on and self._protect_pid == pid:
                log.info(
                    "reflex.worm.stand_down_foreground",
                    pid=pid, exe=exe, cpu_percent=pct,
                )
                return
            if self._is_intentional(exe):
                log.info("worm.skip.intentional", pid=pid, exe=exe, cpu_percent=pct)
                return

        ok = self._throttler.demote(pid, _BELOW_NORMAL)
        self._bus.publish(
            Event(
                topic="reflex.worm.throttle",
                payload={"pid": pid, "exe": exe, "cpu_percent": pct, "ok": ok,
                         "critical": self._thermal_critical},
                ts=time.monotonic(),
            )
        )
        log.info(
            "reflex.worm.throttle",
            pid=pid, exe=exe, cpu_percent=pct, ok=ok,
            critical=self._thermal_critical,
        )

    async def run(self) -> None:
        q = self._bus.subscribe("cpu.spike")
        win_q = self._bus.subscribe("window.foreground")
        crit_q = self._bus.subscribe("thermal.critical")
        prot_q = self._bus.subscribe("cortex.protect_foreground")
        log.info("reflex.worm.start", deadline_ms=int(self._deadline * 1000))
        spike_task: asyncio.Task[Event] = asyncio.create_task(q.get())
        win_task: asyncio.Task[Event] = asyncio.create_task(win_q.get())
        crit_task: asyncio.Task[Event] = asyncio.create_task(crit_q.get())
        prot_task: asyncio.Task[Event] = asyncio.create_task(prot_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {spike_task, win_task, crit_task, prot_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        event = t.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("reflex.worm.recv_failed", error=str(e))
                        event = None

                    if t is spike_task:
                        spike_task = asyncio.create_task(q.get())
                    elif t is win_task:
                        win_task = asyncio.create_task(win_q.get())
                    elif t is crit_task:
                        crit_task = asyncio.create_task(crit_q.get())
                    elif t is prot_task:
                        prot_task = asyncio.create_task(prot_q.get())

                    if event is None:
                        continue

                    if event.topic.startswith("window"):
                        exe = str(event.payload.get("exe", ""))
                        if exe:
                            self.mark_intentional(exe)
                        continue

                    if event.topic == "thermal.critical":
                        self._thermal_critical = bool(event.payload.get("on"))
                        continue

                    if event.topic == "cortex.protect_foreground":
                        self._protect_on = bool(event.payload.get("on"))
                        fg = event.payload.get("foreground_pid")
                        try:
                            self._protect_pid = int(fg) if fg is not None else None
                        except (TypeError, ValueError):
                            self._protect_pid = None
                        continue

                    # else: cpu.spike — run the handler under deadline
                    try:
                        await asyncio.wait_for(self._handle(event), timeout=self._deadline)
                    except asyncio.TimeoutError:
                        log.warning("reflex.worm.deadline_exceeded", event=event.topic)
                    except Exception as e:
                        log.exception("reflex.worm.handler_failed", error=str(e))
        finally:
            for pending in (spike_task, win_task, crit_task, prot_task):
                if not pending.done():
                    pending.cancel()
            self._bus.unsubscribe("cpu.spike", q)
            self._bus.unsubscribe("window.foreground", win_q)
            self._bus.unsubscribe("thermal.critical", crit_q)
            self._bus.unsubscribe("cortex.protect_foreground", prot_q)
```

- [ ] **Step 6.4: Run tests**

Run: `pytest tests/reflexes/test_worm_veto.py tests/reflexes/test_worm.py -v`
Expected: all PASS.

- [ ] **Step 6.5: Run safety audit**

Run: `pytest tests/test_safety_audit.py -v`
Expected: PASS — no new destructive calls.

- [ ] **Step 6.6: Commit**

```bash
rtk git add src/chimera/reflexes/worm.py tests/reflexes/test_worm_veto.py
rtk git commit -m "feat(worm): apply veto precedence (thermal.critical > protect_foreground)"
```

---

## Phase 6 — Lysosome Scavenger

### Task 7: Define `LysosomeBackend` Protocol + Null backend

**Files:**
- Modify: `src/chimera/sensors/base.py` (add Protocol) *or* add `src/chimera/reflexes/base.py`
- Test: `tests/reflexes/test_lysosome.py` (new, partial for this task)

- [ ] **Step 7.1: Write failing skeleton test**

Create `tests/reflexes/test_lysosome.py`:

```python
"""Lysosome scavenger — backend contract + sweep behavior."""
from __future__ import annotations

from chimera.reflexes.lysosome import LysosomeBackend, NullLysosomeBackend


def test_null_backend_is_noop():
    b: LysosomeBackend = NullLysosomeBackend()
    assert b.trim_working_set([1, 2, 3]) == 0
    assert b.flush_system_cache() is None
    assert b.kill(1234) is False
```

- [ ] **Step 7.2: Run to confirm failure**

Run: `pytest tests/reflexes/test_lysosome.py::test_null_backend_is_noop -v`
Expected: FAIL — module `chimera.reflexes.lysosome` does not exist yet.

- [ ] **Step 7.3: Create `lysosome.py` with Protocol + NullBackend**

Create `src/chimera/reflexes/lysosome.py`:

```python
"""Lysosome scavenger — idle-time cleanup reflex.

Three-phase sweep triggered on ``idle.enter``:
  1. Working-set trim (non-destructive; OS re-pages on demand).
  2. System cache flush (admin-only; logs + skips on access-denied).
  3. Opt-in target kill (only exes listed in [lysosome] targets; safety-gated).

See design §5.5. Listed in test_safety_audit's ALLOWED_MODULES because
phase 3 invokes ``proc.kill()`` after passing through ``safety.gate``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterable
from typing import Protocol

import structlog

from chimera.bus import Bus, Event
from chimera.safety import ProtectedSpecies

log = structlog.get_logger(__name__)


class LysosomeBackend(Protocol):
    def trim_working_set(self, pids: Iterable[int]) -> int: ...
    def flush_system_cache(self) -> int | None: ...
    def kill(self, pid: int) -> bool: ...


class NullLysosomeBackend:
    """Non-Windows / test fallback. All phases become no-ops."""

    def trim_working_set(self, pids: Iterable[int]) -> int:
        return 0

    def flush_system_cache(self) -> int | None:
        return None

    def kill(self, pid: int) -> bool:
        return False
```

- [ ] **Step 7.4: Run test**

Run: `pytest tests/reflexes/test_lysosome.py::test_null_backend_is_noop -v`
Expected: PASS.

- [ ] **Step 7.5: Commit**

```bash
rtk git add src/chimera/reflexes/lysosome.py tests/reflexes/test_lysosome.py
rtk git commit -m "feat(lysosome): add backend Protocol and null implementation"
```

### Task 8: Implement LysosomeReflex sweep logic (backend-agnostic)

**Files:**
- Modify: `src/chimera/reflexes/lysosome.py`
- Test: `tests/reflexes/test_lysosome.py`

- [ ] **Step 8.1: Write failing tests**

Append to `tests/reflexes/test_lysosome.py`:

```python
import asyncio
import contextlib
import time
from collections.abc import Iterable
from unittest.mock import MagicMock

import pytest

from chimera.bus import Bus, Event
from chimera.reflexes.lysosome import LysosomeBackend, LysosomeReflex, NullLysosomeBackend
from chimera.safety import ProtectedSpecies


class _RecordingBackend:
    def __init__(self, kill_succeeds: bool = True) -> None:
        self.trims: list[list[int]] = []
        self.flushes: int = 0
        self.kills: list[int] = []
        self._kill_ok = kill_succeeds
    def trim_working_set(self, pids: Iterable[int]) -> int:
        p = list(pids); self.trims.append(p); return len(p)
    def flush_system_cache(self) -> int | None:
        self.flushes += 1; return 0
    def kill(self, pid: int) -> bool:
        self.kills.append(pid); return self._kill_ok


def _make(backend: LysosomeBackend, *, targets: tuple[str, ...] = (),
          enabled: bool = True, interval: int = 0) -> tuple[Bus, LysosomeReflex]:
    bus = Bus()
    safety = ProtectedSpecies.from_list(["winlogon.exe"])
    # Inject a deterministic PID scanner so tests don't touch real psutil.
    def fake_scan() -> list[tuple[int, str]]:
        return [(1234, "hog.exe"), (9999, "winlogon.exe"), (4242, "crash_handler.exe")]
    r = LysosomeReflex(
        bus, safety, backend,
        enabled=enabled,
        sweep_interval_seconds=interval,
        targets=targets,
        pid_scanner=fake_scan,
    )
    return bus, r


@pytest.mark.asyncio
async def test_disabled_performs_no_sweep():
    bus, r = _make(_RecordingBackend(), enabled=False)
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.03)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert r.sweep_count == 0


@pytest.mark.asyncio
async def test_idle_enter_triggers_phases_1_and_2():
    backend = _RecordingBackend()
    bus, r = _make(backend)
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(backend.trims) == 1
    # Non-protected pids only
    assert 9999 not in backend.trims[0]
    assert 1234 in backend.trims[0]
    assert backend.flushes == 1
    assert backend.kills == []  # no targets configured


@pytest.mark.asyncio
async def test_empty_targets_skips_phase_3():
    backend = _RecordingBackend()
    bus, r = _make(backend, targets=())
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert backend.kills == []


@pytest.mark.asyncio
async def test_target_kill_is_safety_gated():
    backend = _RecordingBackend()
    bus, r = _make(backend, targets=("crash_handler.exe", "winlogon.exe"))
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert backend.kills == [4242]  # winlogon.exe (pid 9999) blocked by safety


@pytest.mark.asyncio
async def test_rate_limit_prevents_double_sweep():
    backend = _RecordingBackend()
    bus, r = _make(backend, interval=60)
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.005)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.03)
        bus.publish(Event(topic="idle.enter", payload={}, ts=time.monotonic()))
        await asyncio.sleep(0.03)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert r.sweep_count == 1
```

- [ ] **Step 8.2: Run tests to confirm failure**

Run: `pytest tests/reflexes/test_lysosome.py -v`
Expected: FAIL.

- [ ] **Step 8.3: Implement `LysosomeReflex`**

Append to `src/chimera/reflexes/lysosome.py`:

```python
class LysosomeReflex:
    """Idle-time scavenger. Runs three phases in sequence per sweep."""

    def __init__(
        self,
        bus: Bus,
        safety: ProtectedSpecies,
        backend: LysosomeBackend,
        *,
        enabled: bool = True,
        sweep_interval_seconds: int = 600,
        targets: tuple[str, ...] = (),
        pid_scanner=None,
    ) -> None:
        self._bus = bus
        self._safety = safety
        self._backend = backend
        self._enabled = enabled
        self._interval = sweep_interval_seconds
        self._targets = frozenset(t.lower() for t in targets)
        self._pid_scanner = pid_scanner or _default_pid_scanner
        self._last_sweep: float = 0.0
        self._abort = asyncio.Event()
        self.sweep_count: int = 0

    def _eligible_for_trim(self, procs: list[tuple[int, str]]) -> list[int]:
        return [pid for pid, exe in procs
                if not self._safety.is_protected(exe, pid=pid)]

    def _target_hits(self, procs: list[tuple[int, str]]) -> list[tuple[int, str]]:
        return [(pid, exe) for pid, exe in procs
                if exe.lower() in self._targets]

    async def _sweep(self) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        if self._last_sweep and (now - self._last_sweep) < self._interval:
            log.info("reflex.lysosome.rate_limited", elapsed=now - self._last_sweep)
            return
        self._last_sweep = now
        self._abort.clear()
        self.sweep_count += 1
        procs = list(self._pid_scanner())

        # Phase 1 — working-set trim
        if self._abort.is_set():
            return
        trim_pids = self._eligible_for_trim(procs)
        count = await asyncio.to_thread(self._backend.trim_working_set, trim_pids)
        self._bus.publish(Event(
            topic="lysosome.sweep",
            payload={"phase": "workingset", "count": count, "bytes_freed": None},
            ts=time.monotonic(),
        ))
        log.info("reflex.lysosome.workingset_trimmed", count=count)

        # Phase 2 — system cache flush
        if self._abort.is_set():
            return
        bytes_freed = await asyncio.to_thread(self._backend.flush_system_cache)
        self._bus.publish(Event(
            topic="lysosome.sweep",
            payload={"phase": "cachetrim", "count": 1 if bytes_freed is not None else 0,
                     "bytes_freed": bytes_freed},
            ts=time.monotonic(),
        ))
        log.info("reflex.lysosome.cache_flushed", bytes_freed=bytes_freed)

        # Phase 3 — opt-in target kill
        killed = 0
        for pid, exe in self._target_hits(procs):
            if self._abort.is_set():
                break
            if not self._safety.gate(exe, action="kill", pid=pid):
                continue
            if await asyncio.to_thread(self._backend.kill, pid):
                killed += 1
                log.info("reflex.lysosome.target_killed", pid=pid, exe=exe)
        self._bus.publish(Event(
            topic="lysosome.sweep",
            payload={"phase": "targetkill", "count": killed, "bytes_freed": None},
            ts=time.monotonic(),
        ))

    async def run(self) -> None:
        deep_q = self._bus.subscribe("idle.enter")
        exit_q = self._bus.subscribe("idle.exit")
        log.info(
            "reflex.lysosome.start",
            enabled=self._enabled,
            interval_s=self._interval,
            targets=len(self._targets),
        )
        enter_task: asyncio.Task[Event] = asyncio.create_task(deep_q.get())
        exit_task: asyncio.Task[Event] = asyncio.create_task(exit_q.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {enter_task, exit_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    try:
                        _event = t.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("reflex.lysosome.recv_failed", error=str(e))
                    if t is enter_task:
                        enter_task = asyncio.create_task(deep_q.get())
                        try:
                            await self._sweep()
                        except Exception as e:
                            log.exception("reflex.lysosome.sweep_failed", error=str(e))
                    elif t is exit_task:
                        exit_task = asyncio.create_task(exit_q.get())
                        self._abort.set()
        finally:
            for p in (enter_task, exit_task):
                if not p.done():
                    p.cancel()
            self._bus.unsubscribe("idle.enter", deep_q)
            self._bus.unsubscribe("idle.exit", exit_q)


def _default_pid_scanner() -> list[tuple[int, str]]:
    """Real-world scanner used in production — psutil-backed."""
    try:
        import psutil  # local import so tests don't require it at module load
    except ImportError:
        return []
    out: list[tuple[int, str]] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            info = p.info
            out.append((int(info["pid"]), str(info.get("name") or "")))
        except Exception:
            continue
    return out
```

- [ ] **Step 8.4: Run tests**

Run: `pytest tests/reflexes/test_lysosome.py -v`
Expected: all PASS.

- [ ] **Step 8.5: Commit**

```bash
rtk git add src/chimera/reflexes/lysosome.py tests/reflexes/test_lysosome.py
rtk git commit -m "feat(lysosome): 3-phase sweep (workingset + cache flush + target kill)"
```

### Task 9: Add Win32 backend

**Files:**
- Modify: `src/chimera/reflexes/lysosome.py`

- [ ] **Step 9.1: Write the failing test (platform-gated)**

Append to `tests/reflexes/test_lysosome.py`:

```python
@pytest.mark.windows
def test_win32_backend_importable():
    pytest.importorskip("ctypes")
    from chimera.reflexes.lysosome import Win32LysosomeBackend
    b = Win32LysosomeBackend()
    # Non-existent PID must return 0 trimmed without raising.
    assert b.trim_working_set([4294967290]) == 0
```

- [ ] **Step 9.2: Run — expect skip on non-Windows, fail on Windows until implemented**

Run: `pytest tests/reflexes/test_lysosome.py::test_win32_backend_importable -v`

- [ ] **Step 9.3: Implement Win32 backend**

Append to `src/chimera/reflexes/lysosome.py`:

```python
class Win32LysosomeBackend:
    """Real Windows backend — uses psapi + kernel32 via ctypes."""

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32LysosomeBackend requires Windows")
        import ctypes
        self._kernel32 = ctypes.windll.kernel32
        self._psapi = ctypes.windll.psapi

    def trim_working_set(self, pids: Iterable[int]) -> int:
        trimmed = 0
        for pid in pids:
            h = self._kernel32.OpenProcess(
                self._PROCESS_SET_QUOTA | self._PROCESS_QUERY_INFORMATION,
                False, int(pid),
            )
            if not h:
                continue
            try:
                if self._psapi.EmptyWorkingSet(h):
                    trimmed += 1
            finally:
                self._kernel32.CloseHandle(h)
        return trimmed

    def flush_system_cache(self) -> int | None:
        import ctypes
        # SetSystemFileCacheSize(MinimumFileCacheSize, MaximumFileCacheSize, Flags)
        # Passing (-1, -1, 0) signals "flush and reset to defaults".
        SIZE_T = ctypes.c_size_t
        res = self._kernel32.SetSystemFileCacheSize(
            SIZE_T(-1), SIZE_T(-1), ctypes.c_ulong(0)
        )
        if not res:
            err = self._kernel32.GetLastError()
            log.warning("reflex.lysosome.cache_flush_failed", winerr=err)
            return None
        return 0  # Win API doesn't return bytes freed directly.

    def kill(self, pid: int) -> bool:
        import psutil
        try:
            psutil.Process(int(pid)).kill()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.warning("reflex.lysosome.kill_failed", pid=pid, error=str(e))
            return False


def make_default_lysosome_backend() -> LysosomeBackend:
    if sys.platform == "win32":
        try:
            return Win32LysosomeBackend()
        except Exception as e:
            log.warning("reflex.lysosome.win32_unavailable", error=str(e))
    return NullLysosomeBackend()
```

- [ ] **Step 9.4: Run Linux tests (Windows backend skipped)**

Run: `pytest tests/reflexes/test_lysosome.py -v`
Expected: all non-Windows tests PASS. Windows-marked test skipped on non-Win CI.

- [ ] **Step 9.5: Commit**

```bash
rtk git add src/chimera/reflexes/lysosome.py tests/reflexes/test_lysosome.py
rtk git commit -m "feat(lysosome): add Win32 backend (psapi.EmptyWorkingSet + SetSystemFileCacheSize)"
```

### Task 10: Wire Lysosome into daemon

**Files:**
- Modify: `src/chimera/daemon.py`

- [ ] **Step 10.1: Add import + wiring**

In `src/chimera/daemon.py`:

Add import:
```python
from chimera.reflexes.lysosome import LysosomeReflex, make_default_lysosome_backend
```

In `_start_reflexes`, after the other reflexes are constructed:

```python
lysosome = LysosomeReflex(
    self.bus,
    self.safety,
    make_default_lysosome_backend(),
    enabled=s.lysosome.enabled,
    sweep_interval_seconds=s.lysosome.sweep_interval_seconds,
    targets=s.lysosome.targets,
)
```

Add `("reflex.lysosome", lysosome)` to the spawn list.

- [ ] **Step 10.2: Dry-run smoke test**

Run: `python -m chimera --dry-run`
Expected: heartbeat visible for ~2 s, Ctrl-C clean shutdown, exit 0.

- [ ] **Step 10.3: Commit**

```bash
rtk git add src/chimera/daemon.py
rtk git commit -m "feat(daemon): spawn LysosomeReflex as a supervised task"
```

---

## Phase 7 — Safety Audit Extension

### Task 11: Allowlist Lysosome + gated-kill AST check

**Files:**
- Modify: `tests/test_safety_audit.py`

- [ ] **Step 11.1: Write failing test (allowlist + gated check)**

Replace `tests/test_safety_audit.py`:

```python
"""Static audit: no module may call psutil terminate/kill/suspend/nice outside
``chimera.reflexes.worm``, ``chimera.safety``, or ``chimera.reflexes.lysosome``.

Additionally, every destructive call in ``lysosome`` must be preceded by a
``safety.gate(...)`` call within the same function (see design §7).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "chimera"

DESTRUCTIVE = {"terminate", "kill", "suspend"}
ALLOWED_MODULES = {
    "chimera.reflexes.worm",
    "chimera.safety",
    "chimera.reflexes.lysosome",
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(rel.parts)


def test_no_destructive_calls_outside_allowlist() -> None:
    offenders: list[tuple[str, int, str]] = []
    for py in SRC.rglob("*.py"):
        mod = _module_name(py)
        if mod in ALLOWED_MODULES:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in DESTRUCTIVE:
                    offenders.append((mod, node.lineno, node.func.attr))
    assert not offenders, (
        "Destructive process calls found outside safety-gated modules:\n"
        + "\n".join(f"  {m}:{line} -> .{attr}()" for m, line, attr in offenders)
    )


def test_worm_reflex_uses_safety_gate() -> None:
    src = (SRC / "reflexes" / "worm.py").read_text(encoding="utf-8")
    assert "gate(" in src or "is_protected(" in src, (
        "Worm reflex does not pass through the safety gate — refuse to merge."
    )


def _is_safety_gate_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "gate":
        return True
    if isinstance(func, ast.Name) and func.id == "gate":
        return True
    return False


def _is_destructive_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in DESTRUCTIVE
    )


def test_lysosome_kill_calls_are_gated() -> None:
    tree = ast.parse((SRC / "reflexes" / "lysosome.py").read_text(encoding="utf-8"))
    ungated: list[int] = []
    for func in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        gate_seen = False
        for node in ast.walk(func):
            if _is_safety_gate_call(node):
                gate_seen = True
            elif _is_destructive_call(node):
                if not gate_seen:
                    ungated.append(node.lineno)
    assert not ungated, (
        "Ungated destructive calls in lysosome.py at lines: "
        + ", ".join(str(n) for n in ungated)
    )
```

- [ ] **Step 11.2: Run audit tests**

Run: `pytest tests/test_safety_audit.py -v`
Expected: all PASS (Lysosome's `kill` is gated via `safety.gate` inside `_sweep`).

- [ ] **Step 11.3: Commit**

```bash
rtk git add tests/test_safety_audit.py
rtk git commit -m "test(safety): extend audit to allow and gate-check lysosome.kill"
```

---

## Phase 8 — Elevation (B2)

### Task 12: Installer + task scheduler run at highest privilege

**Files:**
- Modify: `installer/chimera.iss`
- Modify: `installer/chimera.spec`
- Modify: `scripts/install_task.ps1`
- Create: `docs/UPGRADING.md`

- [ ] **Step 12.1: Update `chimera.iss`** (Inno Setup)

Locate the `[Code]` block or `schtasks.exe /Create` section that registers the scheduled task. Ensure the created task has `RunLevel=Highest`:

```pascal
[Code]
procedure RegisterChimeraTask();
var
  ResultCode: Integer;
begin
  { ... existing code ... }
  Exec(
    ExpandConstant('{sys}\schtasks.exe'),
    '/Create /F /TN "ChimeraHomeostasis" /TR "{app}\chimera.exe" '
    + '/SC ONLOGON /RL HIGHEST',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
```

If the install uses PowerShell inline, ensure `-RunLevel Highest` is passed to `New-ScheduledTaskPrincipal`.

- [ ] **Step 12.2: Update `chimera.spec`** (PyInstaller)

Set on the `EXE(...)` definition:

```python
exe = EXE(
    # ...existing args...
    uac_admin=True,
    # ...
)
```

- [ ] **Step 12.3: Update `scripts/install_task.ps1`**

In the `New-ScheduledTaskPrincipal` call, add `-RunLevel Highest`:

```powershell
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest
```

- [ ] **Step 12.4: Create `docs/UPGRADING.md`**

```markdown
# Upgrading Chimera

## 2026-04-19 — Daemon now requires elevation

Starting with the v2 menagerie (biological veto hierarchy), Chimera
runs at the highest available privilege so the Lysosome can call
`SetSystemFileCacheSize` and Worm's `psutil.nice` reliably reaches
foreign processes.

### What you need to do

1. Close any running Chimera instance (tray → Quit, or `taskkill /IM chimera.exe`).
2. Re-run the installer, **or** re-run `scripts\install_task.ps1` from
   an elevated PowerShell. This rewrites the scheduled task with
   `-RunLevel Highest`.
3. Sign out and back in (the task fires at logon).
4. First launch will raise a UAC prompt. Accept.

Subsequent logons run silently — the stored task-scheduler credentials
carry the elevation token.

### Verifying

```powershell
Get-ScheduledTask -TaskName "ChimeraHomeostasis" | Select-Object -ExpandProperty Principal
```

`RunLevel` must be `Highest`.
```

- [ ] **Step 12.5: Local build smoke test (Windows box only)**

Run: `.\installer\build.ps1`
Expected: completes successfully, drops installer artifacts.

- [ ] **Step 12.6: Commit**

```bash
rtk git add installer/chimera.iss installer/chimera.spec scripts/install_task.ps1 docs/UPGRADING.md
rtk git commit -m "build: run daemon elevated; task RunLevel=Highest, uac_admin=True"
```

---

## Phase 9 — Final Verification

### Task 13: Full test suite + lint + type check

- [ ] **Step 13.1: Full pytest**

Run: `pytest -q`
Expected: all PASS. Zero skips on Linux except Windows-marked tests.

- [ ] **Step 13.2: Lint**

Run: `ruff check src tests`
Expected: no errors.

- [ ] **Step 13.3: Type check**

Run: `mypy`
Expected: success.

- [ ] **Step 13.4: Dry-run daemon on Windows box**

Run: `python -m chimera --dry-run`
Look for (in logs):
- `sensor.thermal.online` or `sensor.thermal.lhm_service_missing`
- `reflex.lysosome.start`
- `reflex.worm.start`
- `reflex.zebrafish.start` showing `critical_c=95.0`

Expected: heartbeat stable, no task.error events, clean Ctrl-C.

- [ ] **Step 13.5: Live-fire manual tests (Windows)**

Execute the §9.2 checks from the design spec:
- foreground-chrome stand-down under synthetic spike
- lowered critical threshold triggers `worm.critical_override`
- idle timeout triggers `lysosome.sweep` for all three phases

- [ ] **Step 13.6: Push branch**

```bash
rtk git push -u origin homeostasis
```

(Only push when user has approved ship.)

---

## Open Questions / Deferred

- Dashboard auth token once daemon runs elevated (follow-up spec).
- Real BMTK/OpenWorm/c302 sidecars behind the same Protocol (future).
- ML-backed spike attribution (future).
