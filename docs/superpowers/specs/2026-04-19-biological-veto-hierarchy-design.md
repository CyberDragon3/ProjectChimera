# Project Chimera — Biological Veto Hierarchy (v2 Menagerie)

**Date:** 2026-04-19
**Branch:** homeostasis
**Author:** Terry + Claude (brainstorm)

## 1. Context

Chimera's current daemon has four reflex tiers (Worm / Fly / Zebrafish / Mouse) named after biological frameworks but implemented as hand-coded heuristics. Today they act independently — there is no cross-tier inhibition. Symptoms:

- Worm demotes foreground processes the user is actively driving ("bites active Chrome").
- `thermal: n/a` in the dashboard (LHM sensor returning nothing).
- No hard thermal floor — a runaway heat event only trips slope-based warning.
- No idle-time cleanup.

This spec adds a **Hierarchy of Vetoes** (Zebrafish > Mouse > Worm) plus a Lysosome scavenger, while keeping the biological framework names as *metaphor and naming* only — no neural simulation is introduced.

## 2. Decisions Locked During Brainstorm

| # | Question                           | Choice | Meaning                                                                                        |
|---|------------------------------------|--------|------------------------------------------------------------------------------------------------|
| 1 | Why these frameworks?              | **B**  | Richer behavior. Frameworks = naming + future Protocol slots. No NEURON / MuJoCo / Allen SDK.  |
| 2 | Lysosome scope                     | **B**  | Working-set trim + `SetSystemFileCacheSize` + opt-in target-kill whitelist.                    |
| 3 | Mouse→Worm inhibit mechanism       | **C**  | Global `cortex.protect_foreground` flag; Worm checks foreground PID at decision time.          |
| 4 | Zebrafish critical semantics       | **C**  | Keep slope-based warning; add absolute 95 °C hard floor with 2-sample confirm, 90 °C clear.    |
| 5 | Elevation strategy                 | **B2** | Whole daemon runs elevated via Task Scheduler "at logon" with Highest privileges.              |
| 6 | Decomposition                      | **Z**  | One spec covering all four modules; implementation plan carves per-module phases.              |

## 3. Goals / Non-Goals

### Goals
- Wire Zebrafish > Mouse > Worm veto hierarchy end-to-end across the bus.
- Diagnose and fix `thermal: n/a`.
- Add hard-floor thermal critical state with hysteresis.
- Add Lysosome scavenger triggered on `idle.enter` (Deep Breath).
- Elevate the daemon to admin so `SetSystemFileCacheSize` works.
- Preserve the CI-enforced safety invariant: no `terminate`/`kill`/`suspend` outside approved modules.

### Non-Goals
- No actual neural simulation — no NEURON, MuJoCo, Allen SDK, Bonsai, or c302 in this cycle.
- No Session-0 Windows service (breaks idle/foreground signals per CLAUDE.md).
- No new outbound network calls (LLM gate behavior unchanged).
- No changes to the dashboard/tray surface beyond new event topics.

## 4. Veto Bus Contract

### 4.1 New topics

| Topic                      | Publisher    | Payload                                                    | Purpose                             |
|----------------------------|--------------|------------------------------------------------------------|-------------------------------------|
| `thermal.critical`         | Zebrafish    | `{on: bool, celsius: float, ts: float}`                    | Supreme veto — bypasses inhibition. |
| `cortex.protect_foreground`| Mouse        | `{on: bool, foreground_pid: int \| None, ts: float}`       | Mouse inhibits Worm for active app. |
| `lysosome.sweep`           | Lysosome     | `{phase: "workingset"\|"cachetrim"\|"targetkill", count: int, bytes_freed: int \| None, ts: float}` | Dashboard telemetry. |

### 4.2 Precedence (Worm decision order)

Worm caches the latest value of both inhibition topics. At every `_handle` tick (bounded by `reflex_deadline_ms`):

1. Pick top CPU offender via existing logic.
2. Run `safety.gate(offender.exe, "nice", pid=offender.pid)`. If denied → log only. (Unchanged.)
3. **If** `thermal.critical.on == True` → bypass all inhibition. Proceed to demote. Log `worm.critical_override`.
4. **Else if** `protect_foreground.on == True` and `offender.pid == protect_foreground.foreground_pid` → stand down. Log `worm.stand_down_foreground`. No mutation.
5. **Else** → existing demote path.

### 4.3 State ownership

- Zebrafish **owns** `thermal.critical` state. Only the Zebrafish module publishes it.
- Mouse **owns** `protect_foreground` state. Only the Mouse module publishes it.
- Worm is a pure consumer of both.
- Neither publisher persists across restarts. Worm initialises both caches to `{on: False}`.

## 5. Per-Module Changes

### 5.1 Zebrafish — `BonZebMetabolism`

**Files:** `src/chimera/sensors/thermal.py`, `src/chimera/reflexes/zebrafish.py`.

**`thermal: n/a` diagnosis.** The sensor code itself is healthy. The failure mode is the WMI `root\LibreHardwareMonitor` namespace not being present (LHM service stopped, or installed but not running as admin, or not installed). Add targeted diagnostics on backend init:

- Try `wmi.WMI(namespace=r"root\LibreHardwareMonitor")`. If it raises `wmi.x_wmi` with "Invalid namespace" or similar → log `sensor.thermal.lhm_service_missing` with remediation (`scripts/install_lhm.ps1` + "run LHM as admin").
- On first successful poll, log `sensor.thermal.online` with sensor count — gives a positive signal in the dashboard/logs.
- Retry backend construction once per 60 s if it fails on boot (so starting LHM after Chimera heals the sensor without daemon restart). Implement as a `LhmThermalBackend` wrapper that treats a missing namespace as a deferred-construction error.

**Critical state machine.** Add to the Zebrafish reflex:

- Entry: `thermal_critical_samples` (default 2) consecutive samples ≥ `thermal_critical_c` (default 95.0).
- Exit: `thermal_critical_samples` consecutive samples ≤ `thermal_critical_clear_c` (default 90.0).
- On transition, publish `thermal.critical {on: <bool>, celsius: <latest>, ts: monotonic()}`.
- Existing slope logic (`thermal_slope_c_per_min`) keeps publishing `thermal.rising` independently. Rising ≠ critical.

**Safety against stuck sensors.** If `thermal.critical.on` has been held for > 5 minutes without any supporting slope evidence, log `thermal.critical.suspicious` and auto-clear. Prevents a firmware-stuck 95 °C reading from pinning the system in throttle forever. Config knob: `thermal_critical_max_hold_seconds = 300`.

### 5.2 Mouse — `BMTKCortex`

**File:** `src/chimera/reflexes/mouse.py`.

**Subscriptions:** `cpu.spike`, `window.foreground`.

**State:** `_foreground_pid: int | None`, `_protect_on: bool`.

**Logic:**
- On `window.foreground` event: update `_foreground_pid`. Publish `cortex.protect_foreground {on: False, foreground_pid: <new>}` immediately (clears any stale protection from a prior window). Next spike re-asserts if still warranted.
- On `cpu.spike` event: if `spike.payload["top_pid"] == self._foreground_pid` and `self._foreground_pid is not None`:
  - Set `_protect_on = True`.
  - Publish `cortex.protect_foreground {on: True, foreground_pid: self._foreground_pid}`.
  - Continue to publish existing `cortex.intentional` event for the dashboard ribbon (preserves current behavior).

**CPU spike payload contract.** `cpu.spike` must carry `top_pid` and `top_exe`. Verify the current sensor/reflex publishes these; if not, extend the publisher (source: `sensors/cpu.py` or `reflexes/worm.py` — wherever `cpu.spike` originates).

**Attribution caveat documented in code + spec:** Correlation ≠ causation. A background compiler can be the CPU hog while Chrome has focus — Mouse will wrongly inhibit demotion of the compiler. Accepted for v1. Thermal critical always overrides, so worst case is a warmer laptop, not a meltdown.

### 5.3 Worm — `OpenWormBrainstem`

**File:** `src/chimera/reflexes/worm.py`.

**New subscriptions:** `thermal.critical`, `cortex.protect_foreground`.

**New state:** `_thermal_critical: bool`, `_protect_foreground: bool`, `_protected_pid: int | None`.

**`_handle` changes:** Implement the precedence order from §4.2 inside the existing `asyncio.wait_for(..., reflex_deadline_ms/1000)` envelope. Do **not** add any await points that could exceed the deadline — both new state checks are local dict reads.

**Preserve existing invariants.**
- `safety.gate()` still called for every action (unchanged).
- Protected species frozenset still consulted (unchanged).
- `BELOW_NORMAL_PRIORITY_CLASS` nice call (never kill) unchanged.

### 5.4 Fly — `NeuroMechFlyReflex`

**File:** `src/chimera/reflexes/fly.py` (existing — minimal change).

Fly already republishes arousal/deep-breath state on `idle.enter` / `idle.exit`. Confirm it emits a topic the Lysosome can subscribe to (e.g., `system.deep_breath {on: bool}`). If absent, add.

### 5.5 Lysosome — `LysosomeScavenger` (NEW)

**File:** `src/chimera/reflexes/lysosome.py` (new). Backend Protocol in `src/chimera/reflexes/base.py` or co-located.

**Subscriptions:** `system.deep_breath` (or equivalent from Fly), `idle.exit` (abort signal).

**Sweep phases** — each phase runs sequentially, each checks `self._aborted` between iterations and stops on `idle.exit`:

1. **Working-set trim.** Iterate `psutil.process_iter(['pid','name','status'])` in a thread. Skip protected species. For each eligible proc:
   - `OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)` via ctypes.
   - `psapi.EmptyWorkingSet(handle)`.
   - Close handle.
   - Log aggregate count. Non-destructive — OS re-pages on demand.
2. **System cache flush.** `SetSystemFileCacheSize(kernel32, -1, -1, 0)` via ctypes. Requires admin token (B2 guarantees). Log result. If GetLastError indicates privilege missing or Windows no-op, downgrade to single warning and skip on future sweeps.
3. **Target kill.** For each exe in `[lysosome] targets` config:
   - Find matching procs via `psutil.process_iter`.
   - `safety.gate(exe, "kill", pid=pid)` — must return True.
   - `proc.kill()`.
   - Log `lysosome.target_killed`.
   - If `targets == []` (default), phase 3 is a no-op.

**Rate limit:** at most one sweep per `sweep_interval_seconds` (default 600). Track last-sweep monotonic time in-process.

**Backend Protocol.** Define `LysosomeBackend` with methods `trim_working_set(pids) -> int`, `flush_system_cache() -> int | None`, `kill(pid) -> bool`. Real `Win32LysosomeBackend` + `NullLysosomeBackend` for tests + Linux CI, matching the existing sensor pattern.

## 6. Config Surface

Append to `config/chimera.toml` and mirror in `chimera.config.Settings` pydantic models.

```toml
[thresholds]
# ... existing keys ...
thermal_critical_c = 95.0
thermal_critical_clear_c = 90.0
thermal_critical_samples = 2
thermal_critical_max_hold_seconds = 300

[lysosome]
enabled = true
sweep_interval_seconds = 600
targets = []                  # e.g. ["chrome_crashpad_handler.exe"]
```

New frozen submodel: `LysosomeSettings(_Frozen)` with fields `enabled: bool`, `sweep_interval_seconds: int`, `targets: tuple[str, ...]`. Add to top-level `Settings`. Extend `Thresholds` with the four new keys.

## 7. Safety Audit Changes

`tests/test_safety_audit.py`:

- Extend the existing allowed-destructive-modules set from `{"chimera.reflexes.worm", "chimera.safety"}` to `{"chimera.reflexes.worm", "chimera.safety", "chimera.reflexes.lysosome"}`.
- Add a new test `test_lysosome_kill_calls_are_gated`: AST-walk `src/chimera/reflexes/lysosome.py`. For every `Call` node whose attribute chain ends in `.kill` / `.terminate` / `.suspend`, verify that a `safety.gate(` call appears within the same `FunctionDef` *before* the destructive call in source order. Fails CI if any destructive call is ungated.

The existing CI job `safety-audit` (see `.github/workflows/ci.yml`) continues to run — the new test runs inside that job automatically since it lives in the same test file.

## 8. Elevation (B2)

- **Installer** (`installer/chimera.iss`): in the `[Code]` block that registers the Task Scheduler entry, set `Principal.RunLevel` to `Highest`. PowerShell-equivalent: `$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest`.
- **PowerShell helper** (`scripts/install_task.ps1`): pass `-RunLevel Highest` to `New-ScheduledTaskPrincipal`.
- **PyInstaller spec** (`installer/chimera.spec`): set `uac_admin=True` on the `EXE()` target so the built exe embeds a `requireAdministrator` manifest. A double-click launch now triggers UAC; the scheduled task runs without a prompt because of the task's stored elevation flag.
- **Migration:** existing installs need to re-run `scripts/install_task.ps1` (or re-run the installer). Document in `README.md` or a new `docs/UPGRADING.md` note.

## 9. Testing Strategy

Linux CI (`pytest -q` under `.[dev]`) covers everything that uses `NullLysosomeBackend` / `NullThermalBackend`. Windows integration tests (`@pytest.mark.windows`) cover real WMI + Win32 paths.

### 9.1 New / updated tests

- `tests/sensors/test_thermal.py` (extended)
  - LHM-missing path publishes `sensor.thermal.lhm_service_missing` diagnostic.
  - `thermal.critical` entry after N consecutive samples ≥ threshold.
  - `thermal.critical` exit after N consecutive samples ≤ clear threshold.
  - Auto-clear after `thermal_critical_max_hold_seconds`.

- `tests/reflexes/test_mouse.py` (extended)
  - Spike on foreground PID publishes `cortex.protect_foreground {on: True}`.
  - Spike on non-foreground PID does **not** publish protect=True.
  - `window.foreground` change always publishes `protect_foreground {on: False}`.

- `tests/reflexes/test_worm_veto.py` (new)
  - `thermal.critical.on=True` → Worm demotes even the foreground PID.
  - `protect_foreground.on=True` + offender==fg + no critical → Worm stands down.
  - Both off → existing demote path unchanged.
  - Protected species never demoted regardless of signal state.
  - Deadline honoured: `_handle` returns within `reflex_deadline_ms` under load.

- `tests/reflexes/test_lysosome.py` (new)
  - Sweep aborts on `idle.exit` mid-phase.
  - `enabled=False` → zero sweeps.
  - Rate-limit: two consecutive `system.deep_breath` within `sweep_interval_seconds` → one sweep.
  - Target-kill path invokes `safety.gate` before `.kill()` (mocked backend).
  - Empty `targets` → phase 3 no-op.

- `tests/test_safety_audit.py` (updated)
  - Allowlist includes `chimera.reflexes.lysosome`.
  - New `test_lysosome_kill_calls_are_gated` AST test.

### 9.2 Manual verification (Windows box, post-merge)
- `python -m chimera --dry-run` for 60 s; confirm `sensor.thermal.online` log line.
- Launch a CPU-bound Chrome tab; bring to foreground; trigger a synthetic spike. Confirm Worm logs `worm.stand_down_foreground`.
- Stress the CPU to push temps over 95 °C (or lower the threshold to 75 in local TOML). Confirm `thermal.critical` fires and Worm logs `worm.critical_override`.
- Lock the workstation for > idle threshold. Confirm `lysosome.sweep` events for each phase. Add a fake pest to `targets` and re-verify target-kill.

## 10. Implementation Rollout Order

Zebrafish-first, per user priority. Each step lands on its own branch/commit set where possible.

1. **Thermal diagnostics + `n/a` fix.** Backend retry loop + targeted logging. No other changes.
2. **Config surface.** Extend `Thresholds` + add `LysosomeSettings`. Add TOML keys with safe defaults. Frozen-model tests.
3. **Veto contract + Zebrafish publisher.** Implement `thermal.critical` state machine and publish. No consumer yet.
4. **Mouse publisher.** Add `protect_foreground` publish. Tests.
5. **Worm consumer + precedence logic.** Subscribe, cache, apply order. `test_worm_veto.py`.
6. **Lysosome module.** Backend Protocol, Null + Win32 backends, scavenger reflex. Default `enabled=true` + `targets=[]`, so phase 3 is inert until user curates.
7. **Safety-audit extension.** Only lands alongside Lysosome commit.
8. **Elevation flip.** Installer + spec + PS script + README update. Ship as a separate commit — ops impact.
9. **Integration test pass on real Windows box.**

## 11. Risks & Open Questions

| Risk                                                                      | Mitigation                                                                                                 |
|---------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------|
| B2 runs entire daemon admin → bigger attack surface on dashboard :8765.   | Dashboard already binds `127.0.0.1` only. Consider auth token on `/events` as follow-up (out of scope).    |
| `SetSystemFileCacheSize` is effectively deprecated on Win10+.             | Log+warn once, skip on subsequent sweeps. Working-set trim carries the real benefit.                       |
| Stuck-sensor false-positive on thermal critical.                          | 5-minute auto-clear (§5.1). Slope evidence check as cross-reference.                                       |
| Spike-source attribution is correlative only.                             | Documented. Thermal critical always wins, so worst-case is thermal rises → Zebrafish overrides.            |
| Lysosome target-kill drift (user adds over-broad glob).                   | Exact-match `exe` string only, no glob. Safety gate still runs. Protected species still protected.         |

## 12. Out of Scope / Follow-ups

- Dashboard auth for admin-elevated daemon.
- Real neural-simulation backends swapped via Protocol (BMTK sidecar, c302 sidecar, etc.).
- Cross-session persistence of `protect_foreground` decisions.
- ML-based spike-source attribution (perf counters + call-stack sampling).
- `--headless` daemon mode with structured JSON streaming (for remote dashboards).
