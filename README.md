# Project Chimera

Biologically-inspired OS homeostasis engine for Windows 11.

A Python daemon that maintains system stability through tiered reflex arcs:

| Tier | Module | Role | Speed |
|------|--------|------|-------|
| 3 (Spinal Cord) | Worm, Fly | Pain reflex, idle detection | <10 ms |
| 2 (Sensory Cortex) | Zebrafish, Mouse | Thermal governor, semantic filter | 1–30 s |
| 1 (Frontal Lobe) | Jarvis (LLM) | Narrative + conflict resolution | 2–5 s |

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
