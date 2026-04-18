// Agent-Dashboard — diagnostic dashboard client
(() => {
  "use strict";

  const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  const RASTER_WINDOW_MS = 5000;

  const state = {
    ommatidia: null,       // 16x16 number[][] diff
    pressure: null,         // {cpu, ram, pressure, derivative}
    cursor: null,           // {x, y, vx, vy}
    sugar: 0,
    policy: null,
    execLog: [],            // last 50
    interrupts: [],         // last 50
    spikes: { fly: [], worm: [], mouse: [] },   // ns timestamps
    cpuPainThreshold: 0.85,
    dirty: true,
  };

  // ----- DOM refs ------------------------------------------------------------
  const el = {
    execLog: document.getElementById("exec-log"),
    execMeta: document.getElementById("exec-meta"),
    ommCanvas: document.getElementById("omm-canvas"),
    pressureFill: document.getElementById("pressure-fill"),
    pressureThresh: document.getElementById("pressure-threshold"),
    pressureVal: document.getElementById("pressure-val"),
    pressureDeriv: document.getElementById("pressure-deriv"),
    pressureCpu: document.getElementById("pressure-cpu"),
    pressureRam: document.getElementById("pressure-ram"),
    sugarCanvas: document.getElementById("sugar-canvas"),
    sugarVal: document.getElementById("sugar-val"),
    rasterFly: document.getElementById("raster-fly"),
    rasterWorm: document.getElementById("raster-worm"),
    rasterMouse: document.getElementById("raster-mouse"),
    transMeta: document.getElementById("trans-meta"),
    reflexMeta: document.getElementById("reflex-meta"),
    actionList: document.getElementById("action-list"),
    actionMeta: document.getElementById("action-meta"),
  };

  // ----- WS ------------------------------------------------------------------
  function connect() {
    let ws;
    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      setTimeout(connect, 2000);
      return;
    }
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleFrame(msg);
    });
    ws.addEventListener("close", () => {
      setTimeout(connect, 2000);
    });
    ws.addEventListener("error", () => {
      try { ws.close(); } catch {}
    });
  }

  function handleFrame(msg) {
    if (!msg || typeof msg !== "object") return;
    const { event, data } = msg;
    if (event === "snapshot") {
      applySnapshot(data);
    } else if (event === "executive") {
      pushExecutive(data);
    } else if (event === "reflex") {
      pushInterrupt(data);
    } else if (event === "policy") {
      state.policy = data;
      if (data && data.worm && typeof data.worm.cpu_pain_threshold === "number") {
        state.cpuPainThreshold = data.worm.cpu_pain_threshold;
      }
    }
    state.dirty = true;
  }

  function applySnapshot(data) {
    if (!data) return;
    state.policy = data.policy ?? state.policy;
    if (state.policy && state.policy.worm && typeof state.policy.worm.cpu_pain_threshold === "number") {
      state.cpuPainThreshold = state.policy.worm.cpu_pain_threshold;
    }
    state.pressure = data.pressure ?? null;
    state.cursor = data.cursor ?? null;
    state.sugar = typeof data.sugar_concentration === "number" ? data.sugar_concentration : 0;

    if (data.ommatidia && Array.isArray(data.ommatidia.diff)) {
      state.ommatidia = data.ommatidia.diff;
    } else if (data.ommatidia && Array.isArray(data.ommatidia.luminance)) {
      state.ommatidia = data.ommatidia.luminance;
    }

    if (data.spikes) {
      state.spikes.fly = (data.spikes.fly || []).slice(-300);
      state.spikes.worm = (data.spikes.worm || []).slice(-300);
      state.spikes.mouse = (data.spikes.mouse || []).slice(-300);
    }

    if (Array.isArray(data.recent_interrupts)) {
      // server pushes reflex events independently, but merge to keep fresh on reconnect
      for (const ev of data.recent_interrupts) {
        if (!state.interrupts.find(i => i._id === interruptId(ev))) {
          pushInterrupt(ev, /*silent*/ true);
        }
      }
    }
  }

  function interruptId(ev) {
    return `${ev.module}|${ev.kind}|${ev.t_fire_ns || ev.t_stimulus_ns || 0}`;
  }

  function pushExecutive(ev) {
    if (!ev) return;
    state.execLog.push(ev);
    if (state.execLog.length > 50) state.execLog.shift();
  }

  function pushInterrupt(ev, silent) {
    if (!ev) return;
    ev._id = interruptId(ev);
    state.interrupts.push(ev);
    if (state.interrupts.length > 50) state.interrupts.shift();
  }

  // ----- Render --------------------------------------------------------------
  function render() {
    if (!state.dirty) {
      requestAnimationFrame(render);
      return;
    }
    state.dirty = false;

    renderExecutive();
    renderOmmatidia();
    renderPressure();
    renderSugar();
    renderRasters();
    renderActions();

    requestAnimationFrame(render);
  }

  function renderExecutive() {
    el.execMeta.textContent = `${state.execLog.length} events`;
    const lines = state.execLog.map((ev) => {
      const kind = (ev.kind || "").padEnd(7);
      const text = (ev.text || "").replace(/\n/g, " ");
      return `<div class="log-line"><span class="log-kind">${escapeHtml(kind)}</span>${escapeHtml(text)}</div>`;
    });
    el.execLog.innerHTML = lines.join("");
    el.execLog.scrollTop = el.execLog.scrollHeight;
  }

  function renderOmmatidia() {
    const canvas = el.ommCanvas;
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);

    const grid = state.ommatidia;
    if (!grid || !grid.length) return;
    const rows = grid.length;
    const cols = grid[0].length || 0;
    if (!rows || !cols) return;

    const cellW = w / cols;
    const cellH = h / rows;
    // amber-on-dark colormap for diff; abs value mapped to alpha
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const v = grid[r][c] || 0;
        const a = Math.min(1, Math.abs(v));
        // amber #B8860B -> (184, 134, 11)
        ctx.fillStyle = `rgba(184, 134, 11, ${a})`;
        ctx.fillRect(Math.floor(c * cellW), Math.floor(r * cellH), Math.ceil(cellW), Math.ceil(cellH));
      }
    }
  }

  function renderPressure() {
    const p = state.pressure;
    const v = p ? Math.max(0, Math.min(1, p.pressure || 0)) : 0;
    el.pressureFill.style.height = `${(v * 100).toFixed(1)}%`;
    const painful = v >= state.cpuPainThreshold;
    el.pressureFill.classList.toggle("pain", painful);
    el.pressureThresh.style.bottom = `${(state.cpuPainThreshold * 100).toFixed(1)}%`;
    el.pressureVal.textContent = v.toFixed(2);
    el.pressureDeriv.textContent = p ? (p.derivative || 0).toFixed(2) : "0.00";
    el.pressureCpu.textContent = p ? (p.cpu || 0).toFixed(2) : "0.00";
    el.pressureRam.textContent = p ? (p.ram || 0).toFixed(2) : "0.00";
  }

  function renderSugar() {
    const canvas = el.sugarCanvas;
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    // Gradient background: radial hotspot from target (fake center if unknown)
    const policy = state.policy || {};
    const mouse = policy.mouse || {};
    const tgt = Array.isArray(mouse.track_target_xy) ? mouse.track_target_xy : null;

    const cx = tgt ? clamp((tgt[0] / 1920) * w, 0, w) : w / 2;
    const cy = tgt ? clamp((tgt[1] / 1080) * h, 0, h) : h / 2;

    const g = ctx.createRadialGradient(cx, cy, 4, cx, cy, Math.max(w, h) * 0.8);
    g.addColorStop(0, "rgba(30, 107, 94, 0.9)");
    g.addColorStop(1, "rgba(0, 0, 0, 0.2)");
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);

    // Cursor dot
    if (state.cursor) {
      const x = clamp((state.cursor.x / 1920) * w, 0, w);
      const y = clamp((state.cursor.y / 1080) * h, 0, h);
      ctx.fillStyle = "#F2F2F7";
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
    }

    el.sugarVal.textContent = (state.sugar || 0).toFixed(3);
  }

  function renderRasters() {
    drawRaster(el.rasterFly, state.spikes.fly);
    drawRaster(el.rasterWorm, state.spikes.worm);
    drawRaster(el.rasterMouse, state.spikes.mouse);
  }

  function drawRaster(canvas, spikes) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    ctx.fillStyle = "rgba(0,0,0,0.4)";
    ctx.fillRect(0, 0, w, h);
    if (!spikes || !spikes.length) return;
    // Assume spike values are perf-counter ns; reference is newest spike.
    const latest = spikes[spikes.length - 1];
    const windowNs = RASTER_WINDOW_MS * 1_000_000;
    const t0 = latest - windowNs;
    ctx.strokeStyle = "var(--red)";
    ctx.fillStyle = "#D14B4B";
    for (const t of spikes) {
      if (t < t0) continue;
      const x = ((t - t0) / windowNs) * w;
      ctx.fillRect(x, 4, 2, h - 8);
    }
  }

  function renderActions() {
    el.actionMeta.textContent = `${state.interrupts.length} interrupts`;
    const items = state.interrupts.slice(-50).map((ev) => {
      const latency = Number(ev.latency_us || 0).toFixed(0);
      const mod = (ev.module || "").toString();
      const kind = (ev.kind || "").toString();
      return `<li><span class="badge">${escapeHtml(latency)} µs</span><span class="module">${escapeHtml(mod)}</span><span class="kind">${escapeHtml(kind)}</span></li>`;
    });
    el.actionList.innerHTML = items.join("");
    el.actionList.scrollTop = el.actionList.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // boot
  connect();
  requestAnimationFrame(render);
})();
