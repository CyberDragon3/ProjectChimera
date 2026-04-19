# Project Chimera

Biologically-inspired OS homeostasis engine for Windows 11.

A Python daemon that maintains system stability through tiered reflex arcs:

| Tier | Module | Role | Speed |
|------|--------|------|-------|
| 3 (Spinal Cord) | Worm, Fly | Pain reflex, idle detection | <10 ms |
| 2 (Sensory Cortex) | Zebrafish, Mouse | Thermal governor, semantic filter | 1–30 s |
| 1 (Frontal Lobe) | Jarvis (LLM) | Narrative + conflict resolution | 2–5 s |

## Neuro layer (optional — `[neuro] enabled = true`)

Swaps the hard-coded Zebrafish / Fly / Mouse reflexes for a spiking-neuron model — **102 GLIF neurons total**, pure numpy, 50 Hz tick:

| Animal | Model | Neurons | Role |
|---|---|---|---|
| Worm | Boolean (unchanged) | 0 | <10 ms CPU-spike throttle |
| Zebrafish | GLIF L1 LIF | 1 | Thermal veto — input = dT/dt |
| Fruit Fly | GLIF L1 LIF + Gaussian noise | 1 | Arousal — stochastic idle jitter |
| Mouse | GLIF L3 pop (80E / 20I, p=0.1 sparse) | 100 | Game-mode / creator-app vetoes |
| Dopamine | scalar trace | — | Reward modulates Mouse E-cell gain |

A **hit** = Worm throttled a background spike without the foreground app re-spiking within the window. **Miss** = foreground spike within the window (a real or apparent disturbance). Dopamine level ∈ [0, 1], exponential decay; recent hit streaks raise cortex excitability so the system "hunts" more aggressively after dry spells.

New package lives at `src/chimera/neuro/` (`glif.py`, `dopamine.py`). Zero destructive calls — the neuro layer only publishes bus events; every throttle still goes through `safety.ProtectedSpecies.gate()` in the Worm. Enforced by `tests/neuro/test_safety_audit_neuro.py`.

New bus topics: `neuro.zebrafish.spike`, `neuro.fly.spike`, `neuro.mouse.rate`, `neuro.dopamine`. Dashboard `/state` surfaces them under a `neuro` block.

Fallback: `[neuro] enabled = false` (default) keeps the legacy reflexes — the GLIF path never loads.

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[windows,ui,llm,dev]"
pytest
python -m chimera --dry-run
```

## Safety invariants

1. Every destructive action (`kill`, `terminate`, raise-priority) passes through `safety.is_protected()`.
2. Default action is `nice(BELOW_NORMAL_PRIORITY_CLASS)` — never kill.
3. Sensors read only PIDs, metrics, and window titles. No OCR, no screenshots, no keystrokes.
4. LLM calls never transmit window titles or file paths by default — pre-digested event summaries only.
5. Protected species list is frozen at boot.

See `C:\Users\terry\.claude\plans\i-have-a-new-glowing-aho.md` for full design.
