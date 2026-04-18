# Project Chimera — Bio-OS "Jarvis" Hybrid System

A runnable MVP of the hierarchical bio-inspired control system defined in
[`architecture.md`](architecture.md). Voice/text command → LLM sets thresholds →
simulated animal connectomes (fly / worm / mouse) monitor system metrics
autonomously → threshold breach fires an interrupt → reflex handler runs in
low latency → LLM narrates the action.

Neuromorphic hardware is substituted with Python `asyncio` + NumPy mini
spiking simulators (structurally faithful, not biologically accurate).
Destructive reflex actions are **dry-run only** — nothing is killed, moved,
or mutated on your machine.

---

## Stack
- Python 3.11+
- Local [Ollama](https://ollama.com) with `qwen2.5:0.5b`
- `psutil` (worm somatosensory), `mss` (fly ommatidia), `pynput` (mouse cortex)
- `FastAPI` + `uvicorn` + WebSockets
- Vanilla JS/CSS UI — no build step

## Quick start

```bash
cd ProjectChimera
pip install -r requirements.txt
ollama pull qwen2.5:0.5b        # one-time
ollama serve &                  # background
python -m app.main              # the whole thing
```

Launch behavior:
- **Desktop app window** — primary launch path. The packaged launcher opens
  Chimera in an embedded window and routes first-run users through setup
  before the command surface appears.
- **http://127.0.0.1:8000/** — local fallback URL if the embedded shell is
  unavailable during development or packaging.
- **http://127.0.0.1:8000/dashboard** — 4-panel diagnostic view that mirrors
  the architecture diagram (Executive / Translation / Reflex / Action).

Offline preview of the command bar without a running server:
`open app/dashboard/static/mock.html` — uses a canned WS stream.

---

## Layout

```
app/
  main.py                 # asyncio.gather over the tiers + server
  config.yaml             # every tunable lives here
  contracts.py            # BioPolicy + event dataclasses (frozen interface)
  event_bus.py            # StimulusBus / InterruptBus / ExecutiveBus / PolicyStore / Snapshot
  tier1_executive.py      # Ollama client + parse_intent + explain_reflex
  tier2_translation.py    # screen / psutil / pynput → biological stimuli
  tier3_reflex/
    base.py               # Connectome ABC (refractory, stop-event, publish)
    fly.py                # Drosophila T4/T5 looming (radial-alignment flow)
    worm.py               # C. elegans AVA recoil (sustained pain + sharp poke)
    mouse.py              # mouse cortex predictive-error spike
  actions.py              # reflex handlers (all dry-run)
  dashboard/
    server.py             # FastAPI app + /ws multiplexer + /api/voice stub
    static/
      index.html          # command bar (Whispr-Flow pill)
      dashboard.html      # diagnostic dashboard
      css/{tokens,bar,dash}.css
      js/{bar,dash,mock-ws}.js
      mock.html
  tests/                  # pytest suite (31 tests)
```

## Demo scenarios

1. **Worm CPU watchdog.** Type `Jarvis, raise worm cpu threshold to 95 percent`.
   The LLM emits a JSON patch, `PolicyStore` updates, chips flash. Run
   `python -c "while True: pass"` in another terminal — watch the worm fire
   `ava_recoil` once CPU sustains >95% for 800 ms.
2. **Cursor chemotaxis.** Type `track cursor to (1920, 0)`. The mouse policy
   updates; the dashboard's sugar-gradient panel becomes non-zero near the
   target. (MVP: attractor is visualized, not acted on — mouse cortex still
   fires on prediction error only.)
3. **Screen looming.** Resize a window rapidly in the captured region; the
   fly's radial-alignment score exceeds threshold → `looming` interrupt.
4. **Jerky cursor.** Flick your cursor unpredictably; mouse cortex predictor
   error exceeds threshold → `error_spike`.

## Commands the Executive understands

qwen2.5:0.5b is small; keep commands short and mention the module:

- `Jarvis, tighten worm CPU to 70%`
- `Track cursor to (1920, 0)`
- `Make the fly more sensitive to looming`
- `Ignore small CPU blips` (raises `poke_derivative`)

Parse failures **fall back to the current policy** — the Executive never
crashes the run loop.

## How it works (one paragraph)

`Tier 2` samplers (30 Hz screen, 20 Hz CPU/RAM, 60 Hz cursor) push stimulus
frames to per-module queues on the `StimulusBus`. `Tier 3` connectomes each
own a queue: fly consumes ommatidia frames and scores outward optic flow;
worm consumes pressure samples and tracks dwell + derivative; mouse consumes
cursor samples and runs a constant-velocity predictor. When a connectome
decides to fire it publishes an `InterruptEvent` on the `InterruptBus`. The
main loop dispatches the event to an action handler (all dry-run in MVP),
then — throttled to once per module per 4 s — asks `Tier 1` for a short
natural-language narration, which is published on the `ExecutiveBus`. A
single FastAPI WebSocket multiplexes `policy` / `executive` / `reflex` /
`snapshot` channels to both UIs.

## Latency note (honest)

The architecture spec targets **<10 µs** stimulus→action latency on a
neuromorphic co-processor. This MVP runs on CPython `asyncio` and measures
**single-digit milliseconds** end-to-end — three orders of magnitude off
the hardware target. The `latency_us` badge on each reflex toast shows the
real number; don't read it as the Loihi claim from the architecture doc.

## Tests

```bash
python -m pytest app/tests -q
# 31 passed
```

Coverage:
- `test_intent_parsing.py` (6) — Ollama JSON robustness, parse/merge/fallback.
- `test_samplers.py` (7) — screen/pressure/cursor sampler behavior + sugar helper + clean shutdown.
- `test_reflex_thresholds.py` (10) — all three connectomes: fire, no-fire, refractory, sensitivity modulation, shutdown.
- `test_dashboard_ws.py` (8) — HTTP routes, `/ws` snapshot/executive/reflex frames.

## Non-goals (MVP scope)

- No real neuromorphic hardware (Loihi etc.).
- No actual process killing — `ava_recoil` only logs.
- Voice `/api/voice` endpoint is a stub that echoes `"[voice transcription not wired]"`. Swap in whisper.cpp behind the same shape to go live.
- Not biologically accurate — the connectomes are structural analogues.
- Single-user, single-host, local-only. No cloud LLM gateway.

## Config

Every tunable — model name, ports, sampler rates, thresholds, initial
`BioPolicy` — lives in `app/config.yaml`. The LLM issues *patches* against
the current policy, so runtime changes compose cleanly with your defaults.
