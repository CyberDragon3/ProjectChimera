const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  current: 1,
  completed: new Set(),
  micOk: false,
  modelOk: false,
  ollamaOk: false,
  initialized: false,
};

let step2AutoAdvanceTimer = 0;

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

async function probeStatus() {
  const r = await fetch("/api/setup/status", { cache: "no-store" });
  if (!r.ok) throw new Error(`status HTTP ${r.status}`);
  return r.json();
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

function setSummary(key, text, tone = "pending") {
  const valueEl = $(`#summary-${key}`);
  if (valueEl) valueEl.textContent = text;

  const card = valueEl?.closest(".status-card");
  if (card) card.dataset.tone = tone;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return null;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  const digits = value >= 100 || idx === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[idx]}`;
}

function formatPullStatus(status) {
  if (!status) return "";
  return /[.!?]$/.test(status) ? status : `${status}…`;
}

function resetModelProgress() {
  $("#s2-progress").hidden = true;
  $("#s2-fill").style.width = "0%";
  $("#s2-percent").textContent = "0%";
  $("#s2-digest").textContent = "";
}

function toggleStepButtons(step, disabled) {
  $$(`#s${step}-actions .btn`).forEach((btn) => {
    btn.disabled = disabled;
  });
}

function scheduleStep2AutoAdvance() {
  window.clearTimeout(step2AutoAdvanceTimer);
  step2AutoAdvanceTimer = window.setTimeout(() => {
    step2AutoAdvanceTimer = 0;
    if (state.current === 2 && state.modelOk) {
      advance(2, 450);
    }
  }, 1500);
}

async function runStep1() {
  window.clearTimeout(step2AutoAdvanceTimer);
  step2AutoAdvanceTimer = 0;

  setSummary("ollama", "Checking", "busy");
  setSummary("model", "Waiting", "pending");
  setStepState(1, "busy", "Probing the configured Ollama host now.");
  setStepNote(1, "Chimera only proceeds once the local runtime responds.");
  $("#s1-actions").hidden = true;

  let data;
  try {
    data = await probeStatus();
  } catch (e) {
    state.ollamaOk = false;
    setSummary("ollama", "API error", "error");
    setSummary("model", "Blocked", "pending");
    setStepState(1, "error", `Couldn't reach the setup API: ${e.message}`);
    setStepNote(1, "The browser could not load /api/setup/status.");
    $("#s1-actions").hidden = false;
    return;
  }

  const host = data.ollama?.url || "http://localhost:11434";
  setStepNote(1, `Configured host: ${host}`);

  if (data.ollama?.reachable) {
    state.ollamaOk = true;
    const version = data.ollama.version ? `v${data.ollama.version}` : "reachable";
    setSummary("ollama", version, "ok");

    const versionText = data.ollama.version ? ` (v${data.ollama.version})` : "";
    setStepState(1, "ok", `Ollama responded on ${host}${versionText}.`);

    const modelReady = primeStep2(data.model);
    advance(1, 650);
    if (modelReady) scheduleStep2AutoAdvance();
  } else {
    state.ollamaOk = false;
    setSummary("ollama", "Offline", "error");
    setSummary("model", "Blocked", "pending");
    setStepState(1, "error", "Ollama is not responding yet. Start it locally, then retry.");
    setStepNote(1, `Expected endpoint: ${host}`);
    $("#s1-actions").hidden = false;
  }
}

function primeStep2(modelInfo) {
  const modelName = modelInfo?.model || "qwen2.5:0.5b";
  const sizeText = formatBytes(modelInfo?.size_bytes);

  $("#s2-model").textContent = modelName;
  resetModelProgress();

  if (sizeText) {
    setStepNote(2, `Configured artifact: ${modelName} (${sizeText}) on disk.`);
  } else {
    setStepNote(2, `Configured artifact: ${modelName}.`);
  }

  if (modelInfo?.present) {
    state.modelOk = true;
    setSummary("model", "Ready", "ok");
    setStepState(2, "ok", "Model is already installed and available locally.");
    $("#s2-actions").hidden = true;
    return true;
  }

  state.modelOk = false;
  setSummary("model", "Needs pull", "pending");
  setStepState(2, "pending", "The configured model is not on disk yet.");
  $("#s2-actions").hidden = false;
  return false;
}

function consumePullEvent(ev) {
  if (ev.error) {
    setSummary("model", "Pull failed", "error");
  }

  if (typeof ev.percent === "number") {
    $("#s2-fill").style.width = `${ev.percent}%`;
    $("#s2-percent").textContent = `${ev.percent.toFixed(1)}%`;
  }

  if (ev.digest) {
    $("#s2-digest").textContent = ev.digest.slice(0, 14);
  }

  if (typeof ev.completed === "number" && typeof ev.total === "number" && ev.total > 0) {
    const received = formatBytes(ev.completed);
    const total = formatBytes(ev.total);
    if (received && total) {
      setStepNote(2, `Transferred ${received} of ${total}.`);
    }
  }

  if (ev.status) {
    setStepState(2, "busy", formatPullStatus(ev.status));
  }
}

async function pullModel() {
  resetModelProgress();
  $("#s2-actions").hidden = true;
  $("#s2-progress").hidden = false;
  setSummary("model", "Downloading", "busy");
  setStepState(2, "busy", "Pulling the configured model from Ollama. This can take a few minutes.");
  setStepNote(2, `Target model: ${$("#s2-model").textContent}.`);

  let resp;
  try {
    resp = await fetch("/api/setup/pull_model", { method: "POST" });
  } catch (e) {
    setSummary("model", "Pull failed", "error");
    setStepState(2, "error", `Network error: ${e.message}`);
    setStepNote(2, "The pull request never reached the local setup API.");
    $("#s2-actions").hidden = false;
    $("#s2-progress").hidden = true;
    return;
  }

  if (!resp.ok || !resp.body) {
    setSummary("model", "Pull failed", "error");
    setStepState(2, "error", `Pull failed (HTTP ${resp.status}).`);
    setStepNote(2, "Ollama did not accept the model pull request.");
    $("#s2-actions").hidden = false;
    $("#s2-progress").hidden = true;
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let success = false;
  let lastError = null;

  while (true) {
    const { value, done } = await reader.read();
    buf += decoder.decode(value || new Uint8Array(), { stream: !done });

    const lines = buf.split("\n");
    buf = lines.pop() ?? "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      let ev;
      try {
        ev = JSON.parse(trimmed);
      } catch {
        continue;
      }

      if (ev.error) lastError = ev.error;
      consumePullEvent(ev);
      if (ev.done && !ev.error) success = true;
    }

    if (done) break;
  }

  const tail = buf.trim();
  if (tail) {
    try {
      const ev = JSON.parse(tail);
      if (ev.error) lastError = ev.error;
      consumePullEvent(ev);
      if (ev.done && !ev.error) success = true;
    } catch {
      // Ignore malformed tail fragments from interrupted streams.
    }
  }

  if (success) {
    $("#s2-fill").style.width = "100%";
    $("#s2-percent").textContent = "100%";
    state.modelOk = true;
    setSummary("model", "Ready", "ok");
    setStepState(2, "ok", "Model download complete.");
    setStepNote(2, `${$("#s2-model").textContent} is cached locally and ready.`);
    advance(2, 600);
  } else {
    setSummary("model", "Pull failed", "error");
    setStepState(2, "error", lastError || "Pull did not complete.");
    setStepNote(2, "Retry the pull once Ollama is running and reachable.");
    $("#s2-actions").hidden = false;
  }
}

async function enableMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    state.micOk = false;
    setSummary("mic", "Unavailable", "error");
    setStepState(3, "error", "This browser does not expose microphone capture.");
    setStepNote(3, "Text commands still work without microphone access.");
    return;
  }

  setSummary("mic", "Awaiting permission", "busy");
  setStepState(3, "busy", "Waiting for microphone permission…");
  setStepNote(3, "Your browser should prompt for audio access now.");
  toggleStepButtons(3, true);

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    state.micOk = false;
    setSummary("mic", "Keyboard only", "error");
    setStepState(3, "error", "Microphone access was not granted.");
    setStepNote(3, "You can allow the microphone later from browser settings.");
    toggleStepButtons(3, false);
    return;
  }

  state.micOk = true;
  setSummary("mic", "Live", "ok");
  setStepState(3, "ok", "Microphone confirmed. Voice commands can start immediately.");
  setStepNote(3, "Running a short level check so you can see audio activity.");
  await runMeter(stream, 2000);
  stream.getTracks().forEach((t) => t.stop());
  toggleStepButtons(3, false);
  advance(3, 450);
}

async function runMeter(stream, ms) {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return;

  let ctx;
  try {
    ctx = new AC();
  } catch {
    return;
  }

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
        const value = (data[i] - 128) / 128;
        sum += value * value;
      }

      const rms = Math.sqrt(sum / data.length);
      const pct = Math.min(100, Math.round(rms * 400));
      fill.style.width = `${pct}%`;

      if (performance.now() - start < ms) {
        requestAnimationFrame(tick);
      } else {
        try {
          ctx.close();
        } catch {
          // Ignore close failures from browsers that tear down the context early.
        }
        resolve();
      }
    }

    tick();
  });
}

function skipMic() {
  state.micOk = false;
  setSummary("mic", "Keyboard only", "pending");
  setStepState(3, "pending", "Microphone skipped. Keyboard commands stay available.");
  setStepNote(3, "You can enable voice later from the browser.");
  advance(3, 250);
}

async function launch() {
  const btn = $('[data-action="launch"]');
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Opening Chimera…";
  }

  try {
    const r = await fetch("/api/setup/mark_complete", { method: "POST" });
    const data = await r.json().catch(() => ({}));
    window.location = data.redirect || "/";
  } catch {
    window.location = "/";
  }
}

function onClick(e) {
  const target = e.target.closest("[data-action]");
  if (!target) return;

  const action = target.dataset.action;
  switch (action) {
    case "retry-ollama":
      runStep1();
      break;
    case "pull-model":
      pullModel();
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
      if (prev >= 1 && state.completed.has(prev)) showStep(prev);
      break;
    }
  }
}

function initializeWizard() {
  if (state.initialized) return;
  state.initialized = true;

  setSummary("ollama", "Checking", "busy");
  setSummary("model", "Waiting", "pending");
  setSummary("mic", "Optional", "pending");
  showStep(1);
  runStep1();
}

document.addEventListener("click", onClick);
document.addEventListener("DOMContentLoaded", initializeWizard);

if (document.readyState === "interactive" || document.readyState === "complete") {
  initializeWizard();
}
