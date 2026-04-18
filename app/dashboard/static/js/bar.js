// Chimera orb — voice-first UI. Single reactive sphere, WS-driven.
const $ = (s) => document.querySelector(s);
const orb       = $("#orb");
const caption   = $("#caption");
const conn      = $("#conn");
const policyEl  = $("#policy");
const reflexLog = $("#reflexlog");
const fallback  = $("#fallback");
const drawer    = $("#drawer");
const drawerFrame = $("#drawer-frame");
const drawerBtn = $("#drawer-toggle");
const scrim     = $("#scrim");

const MODULE_COLORS = {
  fly:   "rgba(180,160,255,0.9)",
  worm:  "rgba(255,140,140,0.9)",
  mouse: "rgba(255,200,100,0.9)",
};

// ---------- Orb state ----------
let stateLock = 0;
function setState(s, ttl) {
  orb.dataset.state = s;
  if (ttl) {
    const token = ++stateLock;
    setTimeout(() => { if (token === stateLock) orb.dataset.state = "idle"; }, ttl);
  }
}
function setCaption(text) {
  if (text) { caption.textContent = text; caption.classList.add("show"); }
  else { caption.classList.remove("show"); }
}

// ---------- Policy pill ----------
let lastPolicy = {};
function renderPolicy(p) {
  const fly = p?.fly?.sensitivity ?? null;
  const worm = p?.worm?.cpu_pain_threshold ?? null;
  const mouse = p?.mouse?.track_target_xy ?? null;
  const parts = [
    fly != null   ? `fly ${Number(fly).toFixed(2)}` : "fly —",
    worm != null  ? `worm cpu ${Math.round(Number(worm)*100)}` : "worm —",
    mouse != null ? `mouse tgt ${Array.isArray(mouse) ? mouse.join(",") : "—"}` : "mouse tgt —",
  ];
  policyEl.textContent = parts.join(" · ");
  const changed = JSON.stringify(p) !== JSON.stringify(lastPolicy);
  if (changed) {
    policyEl.classList.add("flash");
    setTimeout(() => policyEl.classList.remove("flash"), 900);
  }
  lastPolicy = { ...p };
}

// ---------- Reflex feedback ----------
function pingReflex(mod, text) {
  const color = MODULE_COLORS[mod] || "rgba(255,255,255,0.8)";
  orb.animate([
    { boxShadow: `0 0 0 0 ${color}` },
    { boxShadow: `0 0 0 24px rgba(0,0,0,0)` },
  ], { duration: 700, easing: "ease-out" });
  const item = document.createElement("div");
  item.className = "item";
  item.dataset.module = mod || "";
  item.textContent = text;
  reflexLog.appendChild(item);
  while (reflexLog.children.length > 3) reflexLog.firstChild.remove();
  setTimeout(() => item.remove(), 7200);
}

// ---------- TTS ----------
let muted = localStorage.getItem("chimera-muted") === "1";
let currentUtterance = null;
let fallbackEnvTimer = null;
function speak(text) {
  if (muted || !("speechSynthesis" in window) || !text) return;
  try { window.speechSynthesis.cancel(); } catch {}
  const u = new SpeechSynthesisUtterance(text);
  const voices = window.speechSynthesis.getVoices();
  const pick = voices.find(v => /en-US/i.test(v.lang) && !v.default) ||
               voices.find(v => /en-US/i.test(v.lang)) ||
               voices[0];
  if (pick) u.voice = pick;
  u.rate = 1.0; u.pitch = 1.0;
  u.onstart = () => {
    setState("speaking");
    clearInterval(fallbackEnvTimer);
    // Soft sine envelope as fallback rhythm
    let t = 0;
    fallbackEnvTimer = setInterval(() => {
      t += 0.12;
      orb.style.setProperty("--orb-env", String(0.5 + 0.5 * Math.sin(t * 5)));
    }, 80);
  };
  u.onboundary = () => {
    // Jolt envelope per word boundary
    orb.style.setProperty("--orb-env", "1");
    setTimeout(() => orb.style.setProperty("--orb-env", "0.3"), 90);
  };
  u.onend = () => {
    clearInterval(fallbackEnvTimer);
    orb.style.setProperty("--orb-env", "0");
    setState("idle");
    setCaption("");
  };
  currentUtterance = u;
  window.speechSynthesis.speak(u);
}
// Some browsers load voices asynchronously
if ("speechSynthesis" in window) window.speechSynthesis.onvoiceschanged = () => {};

// ---------- STT ----------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, listening = false;
function initSTT() {
  if (!SR) return null;
  const r = new SR();
  r.continuous = false;
  r.interimResults = true;
  r.lang = "en-US";
  r.onresult = (e) => {
    let interim = "", final = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) final += t; else interim += t;
    }
    setCaption(final || interim);
    if (final.trim()) submit(final.trim());
  };
  r.onerror = () => { listening = false; setState("error", 1200); setCaption(""); };
  r.onend = () => { listening = false; };
  return r;
}
function startListen() {
  if (listening) return;
  if (!SR) { fallback.classList.add("show"); fallback.focus(); return; }
  if (!rec) rec = initSTT();
  try { rec.start(); listening = true; setState("listening"); setCaption("Listening…"); }
  catch { listening = false; }
}
function stopListen() {
  if (rec && listening) { try { rec.stop(); } catch {} }
  listening = false;
}

// ---------- Submit ----------
async function submit(text) {
  if (!text || !text.trim()) return;
  setState("thinking");
  setCaption("…thinking");
  try {
    const r = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error("bad");
  } catch {
    setState("error", 1200);
    setCaption("offline");
  }
}

// ---------- WS ----------
function onEvent(frame) {
  switch (frame.event) {
    case "policy": renderPolicy(frame.data || {}); break;
    case "executive": {
      const d = frame.data || {};
      if (d.kind === "explain") {
        setCaption(d.text || "");
        speak(d.text || "");
      } else if (d.kind === "prompt") {
        setCaption(d.text || "");
      } else if (d.kind === "status") {
        setState("thinking");
        setCaption(d.text || "…thinking");
      } else if (d.kind === "error") {
        setState("error", 1200);
        setCaption(d.text || "error");
      }
      break;
    }
    case "reflex": {
      const d = frame.data || {};
      const reason = d.payload?.reason || d.kind || "reflex";
      pingReflex(d.module, `${d.module || "?"} · ${reason}`);
      break;
    }
    case "snapshot": break;
  }
}

if (window.__CHIMERA_MOCK__) {
  window.addEventListener("chimera-ws", (e) => onEvent(e.detail));
  conn.dataset.state = "online";
} else {
  let backoff = 500;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { backoff = 500; conn.dataset.state = "online"; };
    ws.onmessage = (ev) => { try { onEvent(JSON.parse(ev.data)); } catch {} };
    ws.onclose = () => {
      conn.dataset.state = "offline";
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 8000);
    };
    ws.onerror = () => ws.close();
  }
  connect();
}

// ---------- Drawer ----------
let drawerOpen = false;
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

// ---------- Orb click ----------
orb.addEventListener("click", () => listening ? stopListen() : startListen());
orb.addEventListener("keydown", (e) => {
  if (e.code === "Space" || e.code === "Enter") { e.preventDefault(); listening ? stopListen() : startListen(); }
});

// ---------- Fallback input ----------
fallback.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    const v = fallback.value.trim();
    fallback.value = "";
    fallback.classList.remove("show");
    if (v) { setCaption(v); submit(v); }
  } else if (e.key === "Escape") {
    fallback.value = "";
    fallback.classList.remove("show");
  }
});

// ---------- Hotkeys ----------
window.addEventListener("keydown", (e) => {
  const tag = (e.target && e.target.tagName) || "";
  const inInput = tag === "INPUT" || tag === "TEXTAREA";
  if (e.code === "Space" && !inInput) {
    e.preventDefault();
    listening ? stopListen() : startListen();
  } else if (e.key === "Escape") {
    if (drawerOpen) toggleDrawer(false);
    else if (listening) { stopListen(); setCaption(""); }
    else setCaption("");
    try { window.speechSynthesis.cancel(); } catch {}
  } else if ((e.key === "d" || e.key === "D") && !inInput) {
    e.preventDefault(); toggleDrawer();
  } else if ((e.key === "m" || e.key === "M") && !inInput) {
    e.preventDefault();
    muted = !muted;
    localStorage.setItem("chimera-muted", muted ? "1" : "0");
    if (muted) { try { window.speechSynthesis.cancel(); } catch {} }
    setCaption(muted ? "muted" : "unmuted");
    setTimeout(() => setCaption(""), 900);
  }
});

// Idle: empty caption
setCaption("");
