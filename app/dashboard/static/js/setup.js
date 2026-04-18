// Project Chimera — first-run setup wizard.
// Flow: (1) pick provider, (2) configure + test, (3) voice, (4) launch.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const PROVIDERS = {
  ollama: {
    label: "Ollama",
    summary: "Local",
    fields: ["host", "model"],
    default: {
      host: "http://localhost:11434",
      model: "qwen2.5:0.5b",
    },
    suggestions: [
      "qwen2.5:0.5b",
      "qwen2.5:3b",
      "llama3.2:1b",
      "llama3.2:3b",
      "phi3:mini",
      "mistral:7b",
    ],
  },
  openai: {
    label: "OpenAI",
    summary: "Cloud",
    fields: ["api_key", "model"],
    default: {
      base_url: "https://api.openai.com/v1",
      model: "gpt-4o-mini",
    },
    suggestions: [
      "gpt-4o-mini",
      "gpt-4o",
      "gpt-4.1-mini",
      "gpt-4.1",
      "o4-mini",
    ],
  },
  anthropic: {
    label: "Anthropic",
    summary: "Cloud",
    fields: ["api_key", "model"],
    default: {
      model: "claude-3-5-haiku-latest",
    },
    suggestions: [
      "claude-3-5-haiku-latest",
      "claude-3-5-sonnet-latest",
      "claude-3-7-sonnet-latest",
      "claude-sonnet-4-5",
      "claude-opus-4-5",
    ],
  },
  openai_compat: {
    label: "OpenAI-compatible",
    summary: "Custom",
    fields: ["base_url", "api_key", "model"],
    default: {
      base_url: "http://localhost:8080/v1",
      model: "",
    },
    suggestions: [
      "llama-3.1-8b-instant",
      "mixtral-8x7b-32768",
      "gemma2-9b-it",
    ],
  },
};

const state = {
  current: 1,
  completed: new Set(),
  provider: null,
  config: {},
  testedOk: false,
  modelPresent: null,
  micOk: false,
  userConfigPath: "",
  initialized: false,
};

// ---------------------------------------------------------------------------
// Step navigation
// ---------------------------------------------------------------------------

function showStep(n) {
  state.current = n;

  $$(".step").forEach((el) => {
    const step = Number(el.dataset.step);
    el.hidden = step !== n;
  });

  $$(".stepper li").forEach((el) => {
    const step = Number(el.dataset.step);
    const isActive = step === n;
    el.classList.toggle("active", isActive);
    el.classList.toggle("done", state.completed.has(step));
    el.toggleAttribute("aria-current", isActive);
  });

  const back = $('[data-action="back"]');
  if (back) back.hidden = n === 1;
}

function markDone(n) {
  state.completed.add(n);
}

function advance(from, delay = 0) {
  markDone(from);
  const next = from + 1;
  if (delay > 0) {
    window.setTimeout(() => showStep(next), delay);
  } else {
    showStep(next);
  }
}

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------

function setSummary(key, text, tone = "pending") {
  const valueEl = $(`#summary-${key}`);
  if (valueEl) valueEl.textContent = text;
  const card = valueEl?.closest(".status-card");
  if (card) card.dataset.tone = tone;
}

function setStepState(step, value, msg) {
  const stEl = $(`#s${step}-state`);
  if (stEl) stEl.dataset.state = value;
  const msgEl = $(`#s${step}-msg`);
  if (msgEl && msg !== undefined) {
    msgEl.textContent = msg;
    msgEl.classList.remove("msg-ok", "msg-error", "msg-busy");
    if (value === "ok") msgEl.classList.add("msg-ok");
    if (value === "error") msgEl.classList.add("msg-error");
    if (value === "busy") msgEl.classList.add("msg-busy");
  }
}

function setStepNote(step, msg) {
  const noteEl = $(`#s${step}-note`);
  if (noteEl && msg !== undefined) noteEl.textContent = msg;
}

// ---------------------------------------------------------------------------
// Step 1 — provider pick
// ---------------------------------------------------------------------------

function wireProviderCards() {
  $$(".provider-card").forEach((card) => {
    card.addEventListener("click", () => selectProvider(card.dataset.provider));
  });
  $$('input[name="provider"]').forEach((input) => {
    input.addEventListener("change", () => selectProvider(input.value));
  });
}

function selectProvider(key) {
  if (!PROVIDERS[key]) return;
  state.provider = key;
  $$(".provider-card").forEach((card) => {
    card.classList.toggle("selected", card.dataset.provider === key);
    const input = card.querySelector('input[type="radio"]');
    if (input) input.checked = card.dataset.provider === key;
  });
  $('[data-action="pick-provider"]').disabled = false;
  setSummary("provider", PROVIDERS[key].label, "ok");
}

// ---------------------------------------------------------------------------
// Step 2 — configure + test
// ---------------------------------------------------------------------------

function primeStep2() {
  if (!state.provider) return;
  const def = PROVIDERS[state.provider];

  $("#s2-provider-name").textContent = def.label;

  const form = $("#config-form");
  const visibleFields = new Set(def.fields);
  $$(".field", form).forEach((field) => {
    field.hidden = !visibleFields.has(field.dataset.field);
  });

  // Wipe then seed defaults.
  form.reset();
  if (def.default.host !== undefined) form.elements["host"].value = def.default.host;
  if (def.default.base_url !== undefined) form.elements["base_url"].value = def.default.base_url;
  if (def.default.model !== undefined) form.elements["model"].value = def.default.model;

  const dl = $("#model-suggestions");
  dl.innerHTML = "";
  (def.suggestions || []).forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m;
    dl.appendChild(opt);
  });

  const pullBtn = $('[data-action="pull-model"]');
  pullBtn.hidden = state.provider !== "ollama";
  $('[data-action="save-and-continue"]').disabled = true;
  state.testedOk = false;
  state.modelPresent = null;

  setSummary("conn", "Waiting", "pending");
  setStepState(2, "pending", `Enter your ${def.label} details and test the connection.`);
  setStepNote(2, `Config is saved to ${state.userConfigPath || "the local user config"}.`);
  $("#pull-progress").hidden = true;
}

function readForm() {
  const form = $("#config-form");
  const data = new FormData(form);
  const out = {
    provider: state.provider,
    model: (data.get("model") || "").toString().trim(),
    api_key: (data.get("api_key") || "").toString().trim(),
    host: (data.get("host") || "").toString().trim(),
    base_url: (data.get("base_url") || "").toString().trim(),
  };
  return out;
}

async function testConnection() {
  const body = readForm();
  if (!body.model) {
    setStepState(2, "error", "Enter a model name first.");
    return;
  }
  if (body.provider !== "ollama" && !body.api_key) {
    setStepState(2, "error", "Paste your API key before testing.");
    return;
  }

  setSummary("conn", "Testing", "busy");
  setStepState(2, "busy", `Probing ${PROVIDERS[body.provider].label}…`);
  $('[data-action="save-and-continue"]').disabled = true;
  $('[data-action="pull-model"]').hidden = true;

  let resp;
  try {
    resp = await fetch("/api/setup/test_provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    setSummary("conn", "Network error", "error");
    setStepState(2, "error", `Network error: ${e.message}`);
    return;
  }

  const data = await resp.json().catch(() => ({}));

  if (body.provider === "ollama") {
    const reachable = !!data.ollama?.reachable;
    const modelPresent = !!data.model?.present;
    state.modelPresent = modelPresent;

    if (!reachable) {
      setSummary("conn", "Offline", "error");
      setStepState(2, "error", "Ollama daemon is not responding. Start it and retry.");
      setStepNote(2, `Host tried: ${body.host}`);
      return;
    }
    const versionText = data.ollama.version ? ` v${data.ollama.version}` : "";
    setSummary("conn", `Reachable${versionText}`, "ok");

    if (!modelPresent) {
      setStepState(2, "pending", `Model ${body.model} is not on disk yet. Download it to continue.`);
      $('[data-action="pull-model"]').hidden = false;
      state.testedOk = false;
      $('[data-action="save-and-continue"]').disabled = true;
      return;
    }
    setStepState(2, "ok", `Daemon online and ${body.model} is cached locally.`);
    state.testedOk = true;
    $('[data-action="save-and-continue"]').disabled = false;
    return;
  }

  // Cloud providers
  const cloud = data.cloud || {};
  if (!cloud.reachable) {
    setSummary("conn", "Unreachable", "error");
    setStepState(2, "error", cloud.error || "Could not reach the provider.");
    return;
  }
  if (!cloud.authenticated) {
    setSummary("conn", "Auth failed", "error");
    setStepState(2, "error", cloud.error || "Authentication failed. Check the key.");
    return;
  }
  setSummary("conn", "Authenticated", "ok");
  if (cloud.model_ok === false) {
    setStepState(2, "error", `Auth OK but ${body.model} isn't listed by this account. Pick a different model.`);
    const dl = $("#model-suggestions");
    dl.innerHTML = "";
    (cloud.models || []).slice(0, 50).forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      dl.appendChild(opt);
    });
    $('[data-action="save-and-continue"]').disabled = true;
    return;
  }
  setStepState(2, "ok", `Authenticated with ${PROVIDERS[body.provider].label}. ${body.model} is available.`);
  state.testedOk = true;
  $('[data-action="save-and-continue"]').disabled = false;
}

async function pullOllamaModel() {
  const body = readForm();
  if (body.provider !== "ollama") return;

  setSummary("conn", "Downloading", "busy");
  $("#pull-progress").hidden = false;
  $("#pull-fill").style.width = "0%";
  $("#pull-percent").textContent = "0%";
  $("#pull-digest").textContent = "";
  setStepState(2, "busy", `Pulling ${body.model} from Ollama. This can take a few minutes.`);
  $('[data-action="pull-model"]').disabled = true;

  let resp;
  try {
    resp = await fetch("/api/setup/pull_model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: body.host, model: body.model }),
    });
  } catch (e) {
    setStepState(2, "error", `Pull network error: ${e.message}`);
    $('[data-action="pull-model"]').disabled = false;
    return;
  }

  if (!resp.ok || !resp.body) {
    setStepState(2, "error", `Pull failed (HTTP ${resp.status}).`);
    $('[data-action="pull-model"]').disabled = false;
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let success = false;
  let lastError = null;

  function consume(ev) {
    if (ev.error) lastError = ev.error;
    if (typeof ev.percent === "number") {
      $("#pull-fill").style.width = `${ev.percent}%`;
      $("#pull-percent").textContent = `${ev.percent.toFixed(1)}%`;
    }
    if (ev.digest) $("#pull-digest").textContent = ev.digest.slice(0, 14);
    if (ev.status) setStepState(2, "busy", `${ev.status}…`);
    if (ev.done && !ev.error) success = true;
  }

  while (true) {
    const { value, done } = await reader.read();
    buf += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buf.split("\n");
    buf = lines.pop() ?? "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try { consume(JSON.parse(trimmed)); } catch { /* ignore */ }
    }
    if (done) break;
  }
  if (buf.trim()) { try { consume(JSON.parse(buf.trim())); } catch { /* ignore */ } }

  $('[data-action="pull-model"]').disabled = false;

  if (success) {
    $("#pull-fill").style.width = "100%";
    $("#pull-percent").textContent = "100%";
    setSummary("conn", "Ready", "ok");
    setStepState(2, "ok", `${body.model} is cached and ready.`);
    state.testedOk = true;
    state.modelPresent = true;
    $('[data-action="pull-model"]').hidden = true;
    $('[data-action="save-and-continue"]').disabled = false;
  } else {
    setSummary("conn", "Pull failed", "error");
    setStepState(2, "error", lastError || "Pull did not complete.");
  }
}

async function saveAndContinue() {
  const body = readForm();
  if (!state.testedOk) {
    setStepState(2, "error", "Run the connection test first.");
    return;
  }

  setStepState(2, "busy", "Saving configuration…");
  $('[data-action="save-and-continue"]').disabled = true;

  let resp;
  try {
    resp = await fetch("/api/setup/save_provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    setStepState(2, "error", `Could not save config: ${e.message}`);
    $('[data-action="save-and-continue"]').disabled = false;
    return;
  }

  if (!resp.ok) {
    setStepState(2, "error", `Save failed (HTTP ${resp.status}).`);
    $('[data-action="save-and-continue"]').disabled = false;
    return;
  }

  state.config = body;
  advance(2, 400);
}

// ---------------------------------------------------------------------------
// Step 3 — voice (unchanged behavior)
// ---------------------------------------------------------------------------

async function enableMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    state.micOk = false;
    setSummary("mic", "Unavailable", "error");
    setStepState(3, "error", "This browser does not expose microphone capture.");
    return;
  }
  setSummary("mic", "Awaiting permission", "busy");
  setStepState(3, "busy", "Waiting for microphone permission…");
  toggleStepButtons(3, true);
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    state.micOk = false;
    setSummary("mic", "Keyboard only", "error");
    setStepState(3, "error", "Microphone access was not granted.");
    toggleStepButtons(3, false);
    return;
  }
  state.micOk = true;
  setSummary("mic", "Live", "ok");
  setStepState(3, "ok", "Microphone confirmed. Voice commands can start immediately.");
  await runMeter(stream, 2000);
  stream.getTracks().forEach((t) => t.stop());
  toggleStepButtons(3, false);
  advance(3, 450);
}

function toggleStepButtons(step, disabled) {
  $$(`#s${step}-actions .btn`).forEach((btn) => { btn.disabled = disabled; });
}

async function runMeter(stream, ms) {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return;
  let ctx;
  try { ctx = new AC(); } catch { return; }
  const src = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  src.connect(analyser);
  const data = new Uint8Array(analyser.fftSize);
  const meter = $("#s3-meter");
  const fill = $("#s3-meter-fill");
  meter.hidden = false;
  const start = performance.now();
  return new Promise((resolve) => {
    function tick() {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i += 1) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / data.length);
      fill.style.width = `${Math.min(100, Math.round(rms * 400))}%`;
      if (performance.now() - start < ms) requestAnimationFrame(tick);
      else { try { ctx.close(); } catch { /* ignore */ } resolve(); }
    }
    tick();
  });
}

function skipMic() {
  state.micOk = false;
  setSummary("mic", "Keyboard only", "pending");
  setStepState(3, "pending", "Microphone skipped. Keyboard commands stay available.");
  advance(3, 250);
}

// ---------------------------------------------------------------------------
// Step 4 — launch
// ---------------------------------------------------------------------------

async function launch() {
  const btn = $('[data-action="launch"]');
  if (btn) { btn.disabled = true; btn.textContent = "Opening Jarvis…"; }
  try {
    const r = await fetch("/api/setup/mark_complete", { method: "POST" });
    const data = await r.json().catch(() => ({}));
    window.location = data.redirect || "/";
  } catch {
    window.location = "/";
  }
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function onClick(e) {
  const target = e.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  switch (action) {
    case "pick-provider":
      if (!state.provider) return;
      markDone(1);
      primeStep2();
      showStep(2);
      break;
    case "back-to-provider":
      showStep(1);
      break;
    case "test-connection":
      testConnection();
      break;
    case "pull-model":
      pullOllamaModel();
      break;
    case "save-and-continue":
      saveAndContinue();
      break;
    case "enable-mic":
      enableMic();
      break;
    case "skip-mic":
      skipMic();
      break;
    case "launch":
      launch();
      break;
    case "back": {
      const prev = state.current - 1;
      if (prev >= 1) showStep(prev);
      break;
    }
    default:
      break;
  }
}

async function hydrateFromStatus() {
  try {
    const r = await fetch("/api/setup/status", { cache: "no-store" });
    if (!r.ok) return;
    const data = await r.json();
    state.userConfigPath = data.config_path || "";
    const hint = $("#config-path-hint");
    if (hint && state.userConfigPath) {
      hint.textContent = `User config: ${state.userConfigPath}`;
    }
    const inline = $("#cfg-path-inline");
    if (inline && state.userConfigPath) {
      inline.textContent = state.userConfigPath;
    }

    // If a provider is already persisted, pre-select it so re-runs are quick.
    if (data.provider && PROVIDERS[data.provider]) {
      selectProvider(data.provider);
    }
  } catch {
    // Non-fatal — the wizard still works with defaults.
  }
}

function initialize() {
  if (state.initialized) return;
  state.initialized = true;
  wireProviderCards();
  setSummary("provider", "Not chosen", "pending");
  setSummary("conn", "Waiting", "pending");
  setSummary("mic", "Optional", "pending");
  showStep(1);
  hydrateFromStatus();
}

document.addEventListener("click", onClick);
document.addEventListener("DOMContentLoaded", initialize);

if (document.readyState === "interactive" || document.readyState === "complete") {
  initialize();
}
