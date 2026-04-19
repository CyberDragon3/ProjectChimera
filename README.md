# Project Chimera

Biologically-inspired OS homeostasis engine for Windows 11.

A Python daemon that keeps your machine stable through tiered reflex arcs — real spiking neurons, a dopamine reward signal, and a safety-gated kill-switch hierarchy. Ships with an interactive browser dashboard and a system-tray app.

## What it does

| Tier | Module | Role | Speed |
|------|--------|------|-------|
| 3 (Spinal Cord) | Worm, Fly | Pain reflex, idle detection | <10 ms |
| 2 (Sensory Cortex) | Zebrafish, Mouse | Thermal governor, semantic filter | 1–30 s |
| 1 (Frontal Lobe) | Jarvis (LLM) | Narrative + conflict resolution | 2–5 s |

Runs as a user-session daemon — not a Windows Service — so it can see idle time and the foreground window (Session 0 can't).

## Neuro layer (ON by default)

102 GLIF spiking neurons total, pure numpy, ~15 µs per Mouse step at 50 Hz:

| Animal | Model | Neurons | Role |
|---|---|---|---|
| Worm | Boolean | 0 | <10 ms CPU-spike throttle (stays fast) |
| Zebrafish | GLIF L1 LIF | 1 | Thermal veto — input = dT/dt |
| Fruit Fly | GLIF L1 LIF + Gaussian noise | 1 | Arousal — stochastic idle jitter |
| Mouse | GLIF L3 pop (80E / 20I sparse) | 100 | Game-mode / creator-app vetoes |
| Dopamine | scalar trace | — | Reward modulates Mouse E-cell gain |

A *hit* = Worm flattened a background spike without the foreground app re-spiking within the window. A *miss* = foreground spike within the window. Dopamine level ∈ [0,1] with exponential decay; recent hit streaks raise cortex excitability so the system "hunts" more aggressively after dry spells.

Neuro package (`src/chimera/neuro/`) is AST-audited in CI to contain zero destructive calls and zero imports of `chimera.safety`. Every throttle still goes through `safety.ProtectedSpecies.gate()` in the Worm.

To disable the neural path and use the legacy hard-coded reflexes, flip `[neuro] enabled = false` in `config/chimera.local.toml` (local override; never edit `config/chimera.toml` directly).

## Quickstart

```powershell
git clone https://github.com/CyberDragon3/ProjectChimera.git
cd ProjectChimera
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[windows,ui,llm,dev]"
python -m chimera
```

Open http://127.0.0.1:8765 — **interactive dashboard** with live CPU/thermal charts, 4 animal organ cards that flash when their neurons fire, dopamine gauge, Mouse population raster, event feed, and runtime toggles.

Tray app (minimizes to system tray):
```powershell
python -m chimera --tray
```

Auto-start at logon:
```powershell
.\scripts\install_task.ps1
```

## Optional "full bio-fidelity" mode

The core daemon runs on pure numpy. For users who want to ground the models in **real connectomics / real Allen Institute cell types / real MuJoCo fruit-fly biomechanics**, install the optional `[brains]` extra:

```powershell
pip install -e ".[windows,ui,llm,dev,brains]"
```

This pulls in three heavy but genuine frameworks:

| Package | What it unlocks | Post-install step |
|---|---|---|
| `owmeta` | C. elegans 302-neuron connectome (Worm grounding) | `owm download` — fetches the OpenWorm database |
| `bmtk` | Allen Institute Brain Modeling Toolkit (Mouse GLIF-3/5 cell library) | Clone [BMTK Examples](https://github.com/AllenInstitute/bmtk), look under `bio_phys/` for `.json` cell templates |
| `flygym[examples]` | MuJoCo-backed NeuroMechFly fruit fly | Install [MuJoCo](https://mujoco.org/download) if `pip` complains |

When the optional frameworks are present, the dashboard's **Brains** panel lights up green for each. They don't run inside the reflex loop (too heavy for 50 Hz real-time) — they provide:

- **owmeta** → connectome data for visualization + offline wiring exports
- **bmtk** → `BmtkAdapter.glif_params_from_dict(cell_json)` converts an Allen cell JSON into `NeuroCfg` params so Mouse cortex runs on real measured biophysics
- **flygym** → reference kinematics for offline Fly behavior demos

Adapter status is visible at `GET /state.brains_available` and rendered as status chips on the dashboard.

### BonZeb (Zebrafish, optional GUI)

BonZeb is a [Bonsai-Rx](https://bonsai-rx.org/) package — a separate .NET application, not a pip dep. If you want zebrafish-style thermal analysis:

1. Install [Bonsai](https://bonsai-rx.org/docs/articles/installation.html).
2. Open Bonsai → Manage Packages → search "BonZeb" → Install.
3. Wire the `thermal.sample` bus topic (served over WebSocket from `/events`) into BonZeb's thermal-mapping workflows.

This is an offline analytics path — the Chimera daemon itself stays pure-Python.

## Safety invariants

1. Every destructive action (`kill`, `terminate`, `suspend`, priority change) passes through `safety.ProtectedSpecies.gate()`.
2. Default action is `nice(BELOW_NORMAL_PRIORITY_CLASS)` — **never kill**.
3. Sensors read only PIDs, metrics, and the foreground-window title. No OCR, no screenshots, no keystrokes.
4. LLM calls never transmit window titles or file paths by default — pre-digested event summaries only.
5. Protected-species list is frozen at boot (`frozenset`) and additionally pins Windows pseudo-pids 0 and 4 in code.
6. The neuro layer cannot issue a destructive call — enforced by a CI AST audit (`tests/neuro/test_safety_audit_neuro.py`).

## Development

```powershell
pytest -q                                 # full suite (133+ tests)
pytest tests/test_safety_audit.py -q      # CI-enforced AST audit
pytest tests/reflexes/ -q                 # reflex coverage
pytest tests/neuro/ -q                    # neuro-layer coverage
ruff check src tests
mypy
```

Dry-run (heartbeat only, no reflexes fire — useful for probing the bus):
```powershell
python -m chimera --dry-run
```

## Design doc

`C:\Users\terry\.claude\plans\i-have-a-new-glowing-aho.md` (three tiers, protected-species semantics, LLM gate, bus taxonomy).
