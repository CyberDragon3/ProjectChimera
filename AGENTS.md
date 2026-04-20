# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

Project Chimera — a biologically-inspired OS homeostasis daemon for Windows 11. Three tiers of reflex arcs (Worm/Fly <10 ms, Zebrafish/Mouse 1–30 s, Jarvis LLM 2–5 s) maintain system stability through non-destructive process-priority steering. Design doc: `C:\Users\terry\.Codex\plans\i-have-a-new-glowing-aho.md`.

## Commands

Run from repo root with `.venv` activated.

```powershell
pip install -e ".[windows,ui,llm,dev]"   # full Windows install
pip install -e ".[dev]"                   # Linux CI / pure-logic only

pytest -q                                 # all tests (asyncio_mode=auto)
pytest tests/test_safety_audit.py -q      # CI-enforced AST audit
pytest tests/reflexes/test_worm.py::test_name   # single test
pytest -m windows                         # Windows-only integration tests

ruff check src tests
mypy                                      # strict, packages=["chimera"]

python -m chimera --dry-run               # heartbeat only, no reflexes
python -m chimera                         # full daemon
python -m chimera --tray                  # tray-app mode (foreground loop)

.\scripts\run_dev.ps1                     # dev convenience wrapper
.\scripts\install_task.ps1                # register Task Scheduler "at logon"
.\scripts\install_lhm.ps1                 # LibreHardwareMonitor (admin, one-time)
.\installer\build.ps1                     # PyInstaller + Inno Setup (or zip fallback)
```

## Architecture

### Core loop: bus + supervised asyncio tasks
`Chimera` (src/chimera/daemon.py) owns one `Bus`, one `ProtectedSpecies`, one `RingBuffer`, and spawns every sensor/reflex/dashboard as a named supervised task. All lifecycle goes through `Chimera.spawn()` (logs `task.start`/`task.cancelled`/`task.error`). Graceful shutdown via `self._stop: asyncio.Event`, wired to SIGINT/SIGTERM (signal.signal() fallback on Windows).

`Bus` (src/chimera/bus.py) is the nervous system — hierarchical topic pub/sub over `asyncio.Queue`. Publish is non-blocking and drops on overflow (telemetry-over-correctness). A subscriber to `cpu` receives `cpu.*`; empty-prefix `""` is the catch-all used by the dashboard SSE stream. Subscribers own their own bounded queue — slow subscribers never block publishers.

### Tiered data flow
```
[sensors] → bus → [reflexes] → bus → [dashboard / LLM gate]
```
- **Tier-3 sensors** (`sensors/cpu.py`, `sensors/idle.py`) poll at 250 ms–1 s and publish `cpu.spike`, `idle.enter/exit`. CPU sensor **must** wrap `psutil.process_iter` in `asyncio.to_thread` — sync iteration blocks the event loop.
- **Tier-2 sensors** (`sensors/thermal.py` via LHM WMI, `sensors/window.py` via `GetForegroundWindow`) poll at 5 s / 1 s and feed the thermal `RingBuffer` + publish `window.foreground`.
- **Reflexes** (`reflexes/{worm,fly,zebrafish,mouse}.py`) subscribe to specific topics, apply logic, and either demote processes (Worm) or republish higher-level events (Fly arousal, Zebrafish thermal-rising, Mouse intentional-tagging). Worm wraps `_handle` in `asyncio.wait_for(..., deadline_ms/1000)` to enforce the <10 ms budget.
- **LLM gate** (`llm/gate.py`) is conditional-invoke with a token-budget circuit breaker (`min_interval_seconds` + `max_daily_calls`). It never receives window titles or file paths — only pre-digested `Brief` summaries. `claude_client.ClaudeAdvisor` uses `cache_control: {"type": "ephemeral"}` on the protected-species system block.

### Sensor backends — Protocol-first
Every sensor splits into a `Sensor` (polls, publishes) and a `Backend` Protocol. Each backend has a real implementation (pywin32/psutil/wmi) and a `Null*Backend` fallback so tests and Linux CI run without Windows. Use `make_default_*_backend()` factories from `daemon.py`.

### Safety — the non-negotiable invariant
Every destructive action passes through `safety.ProtectedSpecies.gate()`. The whitelist is a `frozenset` loaded once at boot (`from_list`) plus hard-coded `_HARDCODED_PIDS = {0, 4}` and `_HARDCODED_EXES = {"system idle process", "system"}` to defend against the Windows PID 0 pseudo-process (which reports ~1400% CPU). The default action is `psutil.Process.nice(BELOW_NORMAL_PRIORITY_CLASS)` — **never** `.kill()`/`.terminate()`/`.suspend()`.

`tests/test_safety_audit.py` statically walks `src/chimera/*.py` AST for `terminate`/`kill`/`suspend` calls and fails CI if any appear outside `chimera.reflexes.worm` or `chimera.safety`. The `safety-audit` job in `.github/workflows/ci.yml` enforces this independently of the main matrix.

When adding new reflex logic: import `safety` and call `safety.gate(exe, action, pid=pid)` before any mutation; returning `False` means the action is denied and must be logged-only.

### Config — frozen pydantic
`chimera.config.Settings` (and every submodel — `ProtectedSpeciesSettings`, `Thresholds`, `Poll`, `Store`, `LLMSettings`, `DashboardSettings`, `LoggingSettings`) inherits `_Frozen(BaseModel)` with `ConfigDict(frozen=True, extra="forbid")`. Lists are `tuple[str, ...]` so the whitelist cannot be mutated post-load. `load_default()` reads `config/chimera.local.toml` first then falls back to `config/chimera.toml`. Never add runtime setters.

### Dashboard & tray
`dashboard/app.py::create_app(bus, thermal_buf)` returns a FastAPI app with `/` (HTMX), `/state` (JSON snapshot), `/events` (SSE, subscribes `""` for everything). `tray.py::run_tray()` spawns the daemon on a background thread because pystray must own the main thread on Windows. Uvicorn is started from inside the daemon event loop with `server.install_signal_handlers = lambda: None` so it doesn't clobber ours.

### Packaging
Hatchling build. `installer/chimera.spec` is the PyInstaller onedir spec — it must list hidden imports for `uvicorn.loops.*`, `uvicorn.protocols.*`, and `uvicorn.lifespan.*`, plus data files for `dashboard/static/` and `config/chimera.toml`. `installer/chimera.iss` (Inno Setup 6) is a per-user install that registers the Task Scheduler entry in `[Code]`. `installer/build.ps1` auto-detects Inno Setup at two well-known paths and falls back to `Compress-Archive`.

Run model is Task Scheduler "at logon", **not** a Windows Service — Session 0 is blind to user idle/foreground-window signals and would break Fly and Mouse.

## Conventions

- Logging is `structlog.get_logger(__name__)` everywhere; format is JSON-lines (switchable via `[logging] format = "console"`).
- All sync psutil/Win32 work goes through `asyncio.to_thread` — blocking the event loop breaks reflex deadlines.
- Sensors/reflexes accept backends via constructor injection; tests pass fakes that implement the Protocol.
- Heartbeat emits a `chimera.heartbeat` event every second with bus-drop count — watch this in `--dry-run` to confirm the loop is alive.
- `pytest-asyncio` is in `asyncio_mode = "auto"` — async tests need no decorator.
