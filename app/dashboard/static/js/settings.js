// Project Chimera — post-onboarding settings view.

const $ = (sel, root = document) => root.querySelector(sel);

const state = {
  llm: null,
  path: "",
};

const PROVIDER_LABEL = {
  ollama: "Ollama (local)",
  openai: "OpenAI",
  anthropic: "Anthropic",
  openai_compat: "OpenAI-compatible",
};

async function hydrate() {
  const r = await fetch("/api/settings", { cache: "no-store" });
  if (!r.ok) return;
  const data = await r.json();
  state.llm = data.llm || {};
  state.path = data.user_config_path || "";
  render();
}

function render() {
  const llm = state.llm || {};
  $("#cur-provider").textContent = PROVIDER_LABEL[llm.provider] || llm.provider || "–";
  $("#cur-model").textContent = llm.model || "–";
  $("#cur-endpoint").textContent =
    llm.provider === "ollama" ? (llm.host || "http://localhost:11434")
    : llm.provider === "anthropic" ? "api.anthropic.com"
    : (llm.base_url || "–");
  $("#cur-key").textContent = llm.api_key_set ? `✓ ${llm.api_key_masked || "stored"}` : "none";
  $("#cur-path").textContent = state.path || "–";

  const form = $("#inline-form");
  if (llm.provider === "ollama") {
    // Ollama doesn't use an API key; show only the model field.
    form.querySelector('[data-field="api_key"]').hidden = true;
    $("#edit-sub").textContent = "Change the local Ollama model tag. Run a new pull from the wizard if it isn't cached yet.";
  } else {
    form.querySelector('[data-field="api_key"]').hidden = false;
    $("#edit-sub").textContent = `Paste a new ${PROVIDER_LABEL[llm.provider] || "provider"} API key. Leave blank to keep the existing key.`;
  }
  form.elements["model"].value = llm.model || "";
  form.elements["api_key"].value = "";
}

async function saveInline() {
  const form = $("#inline-form");
  const body = {
    provider: state.llm.provider,
    model: form.elements["model"].value.trim(),
  };
  const newKey = form.elements["api_key"].value.trim();
  if (newKey) body.api_key = newKey;
  if (state.llm.base_url) body.base_url = state.llm.base_url;
  if (state.llm.host) body.host = state.llm.host;

  $("#inline-msg").textContent = "Saving…";
  const r = await fetch("/api/setup/save_provider", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    $("#inline-msg").textContent = `Save failed (HTTP ${r.status}).`;
    return;
  }
  $("#inline-msg").textContent = "Saved. Restart Chimera for changes to take effect.";
  await hydrate();
}

async function resetOnboarding() {
  if (!confirm("Forget setup and run the wizard again on the next launch?")) return;
  const r = await fetch("/api/setup/reset", { method: "POST" });
  if (r.ok) {
    const data = await r.json().catch(() => ({}));
    window.location = data.redirect || "/setup";
  }
}

function onClick(e) {
  const target = e.target.closest("[data-action]");
  if (!target) return;
  switch (target.dataset.action) {
    case "reconfigure":
      window.location = "/setup";
      break;
    case "rotate-key":
      $("#inline-edit").hidden = false;
      $("#inline-form").elements["api_key"].focus();
      break;
    case "cancel-inline":
      $("#inline-edit").hidden = true;
      $("#inline-msg").textContent = "";
      break;
    case "save-inline":
      saveInline();
      break;
    case "reset-onboarding":
      resetOnboarding();
      break;
    default:
      break;
  }
}

document.addEventListener("click", onClick);
document.addEventListener("DOMContentLoaded", hydrate);
if (document.readyState === "interactive" || document.readyState === "complete") hydrate();
