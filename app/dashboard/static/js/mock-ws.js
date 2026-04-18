// Offline mock harness — drives the orb through state transitions.
(() => {
  const fire = (frame) => window.dispatchEvent(new CustomEvent("chimera-ws", { detail: frame }));

  // 1. Initial policy
  setTimeout(() => fire({
    event: "policy",
    data: {
      fly: { sensitivity: 0.5 },
      worm: { cpu_pain_threshold: 0.85 },
      mouse: { track_target_xy: [1920, 0] }
    }
  }), 300);

  // 2. Worm reflex @ 1.5s with explanation (red ring pulse)
  setTimeout(() => fire({
    event: "reflex",
    data: {
      module: "worm",
      kind: "cpu-clamp",
      payload: { reason: "CPU 92% → clamped" },
      latency_us: 680.2,
      explanation: "Worm reflex pulled CPU cap before the executive noticed."
    }
  }), 1500);

  // 3. Executive explain ~ 2.5s (triggers speaking state + caption)
  setTimeout(() => fire({
    event: "executive",
    data: {
      kind: "explain",
      text: "Detected CPU pressure; worm reflex clamped cap to 80 percent."
    }
  }), 2500);

  // 4. Policy update @ 3.2s
  setTimeout(() => fire({
    event: "policy",
    data: {
      fly: { sensitivity: 0.5 },
      worm: { cpu_pain_threshold: 0.8 },
      mouse: { track_target_xy: [1920, 0] }
    }
  }), 3200);

  // 5. Fly reflex @ 5.5s (purple ring pulse)
  setTimeout(() => fire({
    event: "reflex",
    data: {
      module: "fly",
      kind: "motion-spike",
      payload: { reason: "ΔL>0.4 in 30ms" },
      latency_us: 412.3,
      explanation: "Fly reflex clamped gain before the frame reached the executive."
    }
  }), 5500);
})();
