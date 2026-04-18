const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const orb = $("#orb");
const caption = $("#caption");
const conn = $("#conn");
const policyEl = $("#policy");
const reflexLog = $("#reflexlog");
const fallback = $("#fallback");
const drawer = $("#drawer");
const drawerFrame = $("#drawer-frame");
const drawerBtn = $("#drawer-toggle");
const scrim = $("#scrim");
const transcriptEl = $("#transcript");
const clearLogBtn = $("#clear-log");
const quickButtons = $$("[data-command]");

const MODULE_COLORS = {
  fly: "rgba(180,160,255,0.9)",
  worm: "rgba(255,140,140,0.9)",
  mouse: "rgba(255,200,100,0.9)",
};

const TRANSCRIPT_LABELS = {
  user: "Command",
  assistant: "Jarvis",
  reflex: "Reflex",
  status: "Runtime",
  error: "Issue",
};

let stateLock = 0;
let lastPolicy = {};
let muted = localStorage.getItem("chimera-muted") === "1";
let currentUtterance = null;
let fallbackEnvTimer = null;
let rec = null;
let listening = false;
let drawerOpen = false;
let transcriptPrimed = false;
const transcriptEntries = [];

function setState(state, ttl) {
  orb.dataset.state = state;
  if (!ttl) return;
  const token = ++stateLock;
  setTimeout(() => {
    if (token === stateLock) orb.dataset.state = "idle";
  }, ttl);
}

function setCaption(text) {
  if (text) {
    caption.textContent = text;
    caption.classList.add("show");
  } else {
    caption.classList.remove("show");
  }
}

function renderPolicy(policy) {
  const fly = policy?.fly?.sensitivity ?? null;
  const worm = policy?.worm?.cpu_pain_threshold ?? null;
  const mouse = policy?.mouse?.track_target_xy ?? null;
  const parts = [
    fly != null ? `fly ${Number(fly).toFixed(2)}` : "fly -",
    worm != null ? `worm cpu ${Math.round(Number(worm) * 100)}` : "worm -",
    mouse != null ? `mouse tgt ${Array.isArray(mouse) ? mouse.join(",") : "-"}` : "mouse tgt -",
  ];
  policyEl.textContent = parts.join(" · ");

  const changed = JSON.stringify(policy) !== JSON.stringify(lastPolicy);
  if (changed) {
    policyEl.classList.add("flash");
    setTimeout(() => policyEl.classList.remove("flash"), 900);
  }
  lastPolicy = JSON.parse(JSON.stringify(policy ?? {}));
}

function pingReflex(moduleName, text) {
  const color = MODULE_COLORS[moduleName] || "rgba(255,255,255,0.8)";
  orb.animate(
    [
      { boxShadow: `0 0 0 0 ${color}` },
      { boxShadow: "0 0 0 24px rgba(0,0,0,0)" },
    ],
    { duration: 700, easing: "ease-out" },
  );

  const item = document.createElement("div");
  item.className = "item";
  item.dataset.module = moduleName || "";
  item.textContent = text;
  reflexLog.appendChild(item);
  while (reflexLog.children.length > 3) reflexLog.firstChild.remove();
  setTimeout(() => item.remove(), 7200);
}

function renderTranscript() {
  if (!transcriptEl) return;
  transcriptEl.replaceChildren();

  if (!transcriptEntries.length) {
    const empty = document.createElement("div");
    empty.className = "transcript-entry";
    empty.dataset.role = "status";
    empty.innerHTML = `
      <span class="entry-role">Ready</span>
      <div class="entry-text">Jarvis will log commands, reflexes, and explanations here.</div>
    `;
    transcriptEl.appendChild(empty);
    return;
  }

  for (const entry of transcriptEntries) {
    const item = document.createElement("div");
    item.className = "transcript-entry";
    item.dataset.role = entry.role;

    const role = document.createElement("span");
    role.className = "entry-role";
    role.textContent = TRANSCRIPT_LABELS[entry.role] || "Log";

    const text = document.createElement("div");
    text.className = "entry-text";
    text.textContent = entry.text;

    item.append(role, text);
    transcriptEl.appendChild(item);
  }

  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function addTranscript(role, text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;

  const prev = transcriptEntries[transcriptEntries.length - 1];
  if (prev && prev.role === role && prev.text === trimmed) return;

  transcriptEntries.push({ role, text: trimmed });
  while (transcriptEntries.length > 10) transcriptEntries.shift();
  renderTranscript();
}

function hydrateTranscript(events) {
  if (transcriptPrimed || !Array.isArray(events) || !events.length) return;
  transcriptPrimed = true;

  for (const event of events) {
    if (event.kind === "prompt") addTranscript("user", event.text || "");
    else if (event.kind === "explain") addTranscript("assistant", event.text || "");
    else if (event.kind === "error") addTranscript("error", event.text || "");
    else if ((event.text || "").trim() && event.text !== "thinking" && event.text !== "idle") {
      addTranscript("status", event.text);
    }
  }
}

function clearTranscript() {
  transcriptEntries.length = 0;
  transcriptPrimed = true;
  renderTranscript();
}

function speak(text) {
  if (muted || !("speechSynthesis" in window) || !text) return;
  try {
    window.speechSynthesis.cancel();
  } catch {}

  const utterance = new SpeechSynthesisUtterance(text);
  const voices = window.speechSynthesis.getVoices();
  const pick =
    voices.find((voice) => /en-US/i.test(voice.lang) && !voice.default) ||
    voices.find((voice) => /en-US/i.test(voice.lang)) ||
    voices[0];
  if (pick) utterance.voice = pick;
  utterance.rate = 1.0;
  utterance.pitch = 1.0;

  utterance.onstart = () => {
    setState("speaking");
    clearInterval(fallbackEnvTimer);
    let tick = 0;
    fallbackEnvTimer = setInterval(() => {
      tick += 0.12;
      orb.style.setProperty("--orb-env", String(0.5 + 0.5 * Math.sin(tick * 5)));
    }, 80);
  };

  utterance.onboundary = () => {
    orb.style.setProperty("--orb-env", "1");
    setTimeout(() => orb.style.setProperty("--orb-env", "0.3"), 90);
  };

  utterance.onend = () => {
    clearInterval(fallbackEnvTimer);
    orb.style.setProperty("--orb-env", "0");
    setState("idle");
    setCaption("");
  };

  currentUtterance = utterance;
  window.speechSynthesis.speak(utterance);
}

if ("speechSynthesis" in window) {
  window.speechSynthesis.onvoiceschanged = () => {};
}

// Speech input: prefer the browser's native recognizer when available so
// wake-word + direct command capture work without a cloud transcription
// backend. Fall back to MediaRecorder uploads for embedded shells and other
// browsers that only expose raw audio capture.

const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition || null;
const speechRecognitionSupported = typeof SpeechRecognitionCtor === "function";
const WAKE_WORD_DELAY_MS = 1200;
const WAKE_RESTART_MS = 800;
const WAKE_WORD_RE = /\b(?:hey\s+)?jarvis\b[\s,:;.-]*(.*)$/i;

let mediaStream = null;
let mediaRecorder = null;
let audioChunks = [];
let speechRecognizer = null;
let wakeRecognizer = null;
let wakeRetryTimer = null;
let wakeRecognitionBlocked = false;
let activeListenMode = null;
const mediaSupported = typeof window.MediaRecorder !== "undefined"
  && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

function extractWakeCommand(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return null;
  const match = trimmed.match(WAKE_WORD_RE);
  if (!match) return null;
  return {
    command: (match[1] || "").trim(),
    heard: trimmed,
  };
}

function normalizeRecognizedCommand(text) {
  const wake = extractWakeCommand(text);
  if (wake) return wake.command;
  return (text || "").trim();
}

function stopWakeRecognition() {
  if (wakeRetryTimer) {
    clearTimeout(wakeRetryTimer);
    wakeRetryTimer = null;
  }
  if (!wakeRecognizer) return;
  const active = wakeRecognizer;
  wakeRecognizer = null;
  try {
    active.onresult = null;
    active.onerror = null;
    active.onend = null;
    active.stop();
  } catch {}
}

function queueWakeRecognition(delay = WAKE_RESTART_MS) {
  if (!speechRecognitionSupported || wakeRecognitionBlocked) return;
  if (wakeRecognizer || listening || document.hidden) return;
  if (wakeRetryTimer) clearTimeout(wakeRetryTimer);
  wakeRetryTimer = window.setTimeout(() => {
    wakeRetryTimer = null;
    startWakeRecognition();
  }, delay);
}

function startWakeRecognition() {
  if (!speechRecognitionSupported || wakeRecognitionBlocked || wakeRecognizer || listening || document.hidden) {
    return false;
  }

  let recognizer;
  try {
    recognizer = new SpeechRecognitionCtor();
  } catch {
    wakeRecognitionBlocked = true;
    return false;
  }

  recognizer.continuous = true;
  recognizer.interimResults = false;
  recognizer.lang = "en-US";
  recognizer.maxAlternatives = 1;

  recognizer.onresult = (event) => {
    let transcript = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i];
      if (!result.isFinal) continue;
      transcript += `${result[0]?.transcript || ""} `;
    }
    transcript = transcript.trim();
    const wake = extractWakeCommand(transcript);
    if (!wake) return;

    stopWakeRecognition();
    if (wake.command) {
      setCaption(wake.command);
      addTranscript("user", wake.command);
      submit(wake.command);
      return;
    }

    startListen({ fromWakeWord: true });
  };

  recognizer.onerror = (event) => {
    const error = event?.error || "speech";
    wakeRecognizer = null;
    if (["not-allowed", "service-not-allowed", "audio-capture"].includes(error)) {
      wakeRecognitionBlocked = true;
      return;
    }
    queueWakeRecognition();
  };

  recognizer.onend = () => {
    wakeRecognizer = null;
    queueWakeRecognition();
  };

  try {
    recognizer.start();
    wakeRecognizer = recognizer;
    return true;
  } catch {
    wakeRecognitionBlocked = true;
    wakeRecognizer = null;
    return false;
  }
}

function stopSpeechRecognition() {
  if (!speechRecognizer) return;
  const active = speechRecognizer;
  speechRecognizer = null;
  try {
    active.onresult = null;
    active.onerror = null;
    active.onend = null;
    active.stop();
  } catch {}
}

function startSpeechRecognitionListen({ fromWakeWord = false } = {}) {
  if (!speechRecognitionSupported || speechRecognizer) return false;

  stopWakeRecognition();

  let recognizer;
  try {
    recognizer = new SpeechRecognitionCtor();
  } catch {
    return false;
  }

  recognizer.continuous = false;
  recognizer.interimResults = false;
  recognizer.lang = "en-US";
  recognizer.maxAlternatives = 1;

  let transcript = "";
  let settled = false;

  const finish = () => {
    listening = false;
    activeListenMode = null;
    speechRecognizer = null;
  };

  recognizer.onresult = (event) => {
    transcript = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      transcript += `${event.results[i][0]?.transcript || ""} `;
    }
  };

  recognizer.onerror = () => {
    if (settled) return;
    settled = true;
    finish();

    if (mediaSupported && !fromWakeWord) {
      startMediaRecorderListen();
      return;
    }

    setState("error", 1400);
    setCaption(fromWakeWord ? "Wake word heard, but the command was lost." : "Voice recognition failed.");
    queueWakeRecognition();
  };

  recognizer.onend = () => {
    if (settled) return;
    settled = true;
    const command = normalizeRecognizedCommand(transcript);
    finish();

    if (command) {
      setCaption(command);
      addTranscript("user", command);
      submit(command);
      return;
    }

    setState("idle");
    setCaption(fromWakeWord ? "Didn't catch the command." : "Didn't catch that.");
    queueWakeRecognition();
  };

  try {
    recognizer.start();
    speechRecognizer = recognizer;
    activeListenMode = "speech";
    listening = true;
    setState("listening");
    setCaption(fromWakeWord ? "Yes?" : "Listening…");
    return true;
  } catch {
    finish();
    return false;
  }
}

async function ensureStream() {
  if (mediaStream) return mediaStream;
  if (!mediaSupported) return null;
  mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  return mediaStream;
}

async function startMediaRecorderListen() {
  if (listening) return;

  stopWakeRecognition();

  if (!mediaSupported) {
    setCaption("Voice not available here — type instead.");
    fallback.classList.add("show");
    fallback.focus();
    return;
  }

  let stream;
  try {
    stream = await ensureStream();
  } catch {
    setState("error", 1400);
    setCaption("Microphone blocked. Type instead.");
    fallback.classList.add("show");
    fallback.focus();
    queueWakeRecognition();
    return;
  }
  if (!stream) {
    setCaption("Voice not available here — type instead.");
    queueWakeRecognition();
    return;
  }

  const mimeCandidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  const mime = mimeCandidates.find((m) => window.MediaRecorder.isTypeSupported?.(m)) || "";
  try {
    mediaRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  } catch {
    setState("error", 1400);
    setCaption("Could not start recording.");
    queueWakeRecognition();
    return;
  }

  audioChunks = [];
  activeListenMode = "media";
  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) audioChunks.push(e.data);
  };
  mediaRecorder.onstop = () => {
    activeListenMode = null;
    const blob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || "audio/webm" });
    audioChunks = [];
    if (blob.size < 1024) {
      setState("idle");
      setCaption("Didn't catch that.");
      queueWakeRecognition();
      return;
    }
    transcribeAndSubmit(blob);
  };

  try {
    mediaRecorder.start();
    listening = true;
    setState("listening");
    setCaption("Listening…");
  } catch {
    listening = false;
    activeListenMode = null;
    setState("error", 1200);
    queueWakeRecognition();
  }
}

async function startListen(options = {}) {
  if (listening) return;
  if (startSpeechRecognitionListen(options)) return;
  await startMediaRecorderListen();
}

function stopListen() {
  if (!listening) return;
  listening = false;
  if (activeListenMode === "speech") {
    try { speechRecognizer?.stop(); } catch { /* ignore */ }
    return;
  }
  try { mediaRecorder?.stop(); } catch { /* ignore */ }
}

async function transcribeAndSubmit(blob) {
    setState("thinking");
    setCaption("Transcribing…");

  const form = new FormData();
  const filename = (blob.type || "").includes("mp4") ? "voice.m4a" : "voice.webm";
  form.append("file", blob, filename);

  let resp;
  try {
    resp = await fetch("/api/voice", { method: "POST", body: form });
  } catch {
    setState("error", 1400);
    setCaption("Voice upload failed.");
    return;
  }

  const data = await resp.json().catch(() => ({}));
  if (!data.ok || !data.text) {
    setState("error", 1800);
    setCaption(data.error || "Transcription unavailable. Type instead.");
    fallback.classList.add("show");
    fallback.focus();
    queueWakeRecognition();
    return;
  }

  setCaption(data.text);
  addTranscript("user", data.text);
  submit(data.text);
}

async function submit(text) {
  if (!text || !text.trim()) return;
  setState("thinking");
  setCaption("Thinking...");

  try {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) throw new Error("bad");
  } catch {
    setState("error", 1200);
    setCaption("offline");
    addTranscript("error", "Command could not reach the local runtime.");
  } finally {
    queueWakeRecognition();
  }
}

function applyStatus(text) {
  const normalized = (text || "").trim().toLowerCase();
  if (normalized === "thinking") {
    setState("thinking");
    setCaption("Thinking...");
    return;
  }
  if (normalized === "idle") {
    setState("idle");
    return;
  }

  setState("idle");
  setCaption(text || "");
  addTranscript("status", text || "");
}

function onEvent(frame) {
  switch (frame.event) {
    case "policy":
      renderPolicy(frame.data || {});
      break;
    case "executive": {
      const data = frame.data || {};
      if (data.kind === "prompt") {
        setCaption(data.text || "");
        addTranscript("user", data.text || "");
      } else if (data.kind === "explain") {
        setCaption(data.text || "");
        addTranscript("assistant", data.text || "");
        speak(data.text || "");
      } else if (data.kind === "tool_ok") {
        const tool = data.data?.tool || "tool";
        const label = tool === "reply" ? (data.text || "") : `${tool}: ${data.text || "done"}`;
        setCaption(label);
        addTranscript("assistant", label);
        if (tool === "reply" && data.text) speak(data.text);
      } else if (data.kind === "tool_err") {
        const tool = data.data?.tool || "tool";
        const msg = `${tool} failed: ${data.text || "error"}`;
        setState("error", 1400);
        setCaption(msg);
        addTranscript("error", msg);
      } else if (data.kind === "status") {
        applyStatus(data.text || "");
      } else if (data.kind === "error") {
        setState("error", 1200);
        setCaption(data.text || "error");
        addTranscript("error", data.text || "error");
      }
      break;
    }
    case "reflex": {
      const data = frame.data || {};
      const reason = data.payload?.reason || data.kind || "reflex";
      const message = `${data.module || "?"} · ${reason}`;
      pingReflex(data.module, message);
      addTranscript("reflex", message);
      break;
    }
    case "snapshot": {
      const data = frame.data || {};
      if (data.policy) renderPolicy(data.policy);
      hydrateTranscript(data.recent_executive || []);
      break;
    }
  }
}

if (window.__CHIMERA_MOCK__) {
  window.addEventListener("chimera-ws", (event) => onEvent(event.detail));
  conn.dataset.state = "online";
} else {
  let backoff = 500;

  function connect() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${location.host}/ws`);

    ws.onopen = () => {
      backoff = 500;
      conn.dataset.state = "online";
    };

    ws.onmessage = (event) => {
      try {
        onEvent(JSON.parse(event.data));
      } catch {}
    };

    ws.onclose = () => {
      conn.dataset.state = "offline";
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 8000);
    };

    ws.onerror = () => ws.close();
  }

  connect();
}

function toggleDrawer(force) {
  drawerOpen = force === undefined ? !drawerOpen : !!force;
  drawer.classList.toggle("open", drawerOpen);
  scrim.classList.toggle("show", drawerOpen);
  drawer.setAttribute("aria-hidden", drawerOpen ? "false" : "true");
  if (drawerOpen && drawerFrame.src === "about:blank") {
    drawerFrame.src = "/dashboard";
  }
}

drawerBtn.addEventListener("click", () => toggleDrawer());
scrim.addEventListener("click", () => toggleDrawer(false));

quickButtons.forEach((button) => {
  button.addEventListener("click", () => submit(button.dataset.command || ""));
});

if (clearLogBtn) clearLogBtn.addEventListener("click", clearTranscript);

orb.addEventListener("click", () => (listening ? stopListen() : startListen()));
orb.addEventListener("keydown", (event) => {
  if (event.code === "Space" || event.code === "Enter") {
    event.preventDefault();
    listening ? stopListen() : startListen();
  }
});

fallback.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    const value = fallback.value.trim();
    fallback.value = "";
    fallback.classList.remove("show");
    if (value) {
      setCaption(value);
      submit(value);
    }
  } else if (event.key === "Escape") {
    fallback.value = "";
    fallback.classList.remove("show");
  }
});

window.addEventListener("keydown", (event) => {
  const tag = event.target?.tagName || "";
  const inInput = tag === "INPUT" || tag === "TEXTAREA";

  if (event.code === "Space" && !inInput) {
    event.preventDefault();
    listening ? stopListen() : startListen();
  } else if (event.key === "Escape") {
    if (drawerOpen) toggleDrawer(false);
    else if (listening) {
      stopListen();
      setCaption("");
    } else {
      setCaption("");
    }
    try {
      window.speechSynthesis.cancel();
    } catch {}
  } else if ((event.key === "d" || event.key === "D") && !inInput) {
    event.preventDefault();
    toggleDrawer();
  } else if ((event.key === "m" || event.key === "M") && !inInput) {
    event.preventDefault();
    muted = !muted;
    localStorage.setItem("chimera-muted", muted ? "1" : "0");
    if (muted) {
      try {
        window.speechSynthesis.cancel();
      } catch {}
    }
    setCaption(muted ? "muted" : "unmuted");
    setTimeout(() => setCaption(""), 900);
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopWakeRecognition();
  else queueWakeRecognition();
});

renderTranscript();
setCaption("");
queueWakeRecognition(WAKE_WORD_DELAY_MS);
