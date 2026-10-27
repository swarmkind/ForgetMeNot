"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const intervals = {60:"every minute",300:"every 5 minutes",900:"every 15 minutes",3600:"hourly",21600:"every 6 hours",86400:"daily",604800:"weekly",2592000:"every 30 days"};
const channelNames = {email:"Email",telegram:"Telegram",push:"Push notification"};
let state = null;
let currentView = "watches";
let currentFilter = "all";
let editing = null;
let refreshing = false;
let toastTimer;
let typingTimer;
let listening = false;
let watchSubmitting = false;
let lastSavedId = null;
let lensError = "";
let serverOffline = false;
let watchCatalog = [];
let catalogLoading = false;

function modelKey(model) {
  return model?.id ? JSON.stringify({provider:model.provider,id:model.id}) : "";
}

function renderWatchModels(preferred) {
  const select = $("#watch-model");
  const previous = preferred || select.value || modelKey(state?.settings.model);
  const choices = new Map();
  for (const model of [state?.settings.model,...watchCatalog,...(state?.watches || []).map((watch) => watch.model)]) {
    if (model?.id) choices.set(modelKey(model), model);
  }
  select.replaceChildren(new Option(choices.size ? "Choose a model" : "Connect a model", ""));
  const names = {codex:"ChatGPT · Codex",chatgpt:"ChatGPT",openrouter:"OpenRouter",claude:"Claude",gemini_cli:"Gemini CLI"};
  for (const [key, model] of choices) select.add(new Option(`${names[model.provider]} · ${model.name || model.id}`, key));
  select.value = choices.has(previous) ? previous : choices.keys().next().value || "";
  updateLens();
}

async function loadWatchModels() {
  if (!state || catalogLoading) return;
  catalogLoading = true;
  try {
    const settings = state.settings;
    const connected = [settings.codex_connected && "codex",settings.openrouter_connected && "openrouter",
      settings.claude_connected && "claude",settings.chatgpt_selected && "chatgpt",settings.gemini_available && "gemini_cli"].filter(Boolean);
    const results = await Promise.allSettled(connected.map(async (provider) => {
      const data = await api(`/api/models/${provider}`);
      return data.models.map((model) => ({provider,...model}));
    }));
    watchCatalog = results.flatMap((result) => result.status === "fulfilled" ? result.value : []);
    renderWatchModels();
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length && !watchCatalog.length) toast(failures[0].reason.message, true);
  } finally { catalogLoading = false; }
}

function updateDeliveryHint() {
  if (!state) return;
  const settings = state.settings;
  const ready = {email:!!(settings.email && settings.smtp_ready),telegram:!!(settings.telegram_chat && settings.telegram_token_set),push:!!settings.ntfy_topic};
  const missing = $("#notification").value.split(",").filter((channel) => channel && !ready[channel]);
  $("#configure-delivery").hidden = !missing.length;
  $("#configure-delivery").textContent = missing.length ? `Set up ${missing.map((channel) => channelNames[channel]).join(" and ")} in Settings ↗` : "";
}

function updateLens() {
  const lens = lensState({state, listening, submitting:watchSubmitting, savedId:lastSavedId, error:lensError, offline:serverOffline, modelReady:!!$("#watch-model").value});
  $("#mascot").dataset.mode = lens.mode;
  document.body.dataset.lensMode = lens.mode;
  $("#input-mode").textContent = lens.mode === "listening" ? "LISTENING" : lens.mode === "locking" ? "FOCUSING" : "";
  for (const [id, text] of [["#lens-mode",lens.status],["#lens-title",lens.title],["#lens-detail",lens.detail]]) {
    if ($(id).textContent !== text) $(id).textContent = text;
  }
  $("#machine-count").textContent = String(state?.watches.filter((watch) => watch.status === "active").length || 0).padStart(2,"0");
  $("#machine-worker").textContent = serverOffline ? "OFFLINE" : state?.worker.error ? "ATTENTION" : state?.worker.running ? "ONLINE" : "OFFLINE";
}

function toast(message, error = false) {
  $("#toast").textContent = message;
  $("#toast").classList.toggle("error", error);
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, error ? 11000 : 4500);
}

async function api(path, method = "GET", body) {
  let response;
  try {
    response = await fetch(path, {method, headers:{"Content-Type":"application/json","X-ForgetMeNot":"1"},
      body: body === undefined ? undefined : JSON.stringify(body)});
  } catch {
    throw new Error("The server could not be reached. Saved watches remain on your server.");
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    $("#app").hidden = true;
    $("#login-view").hidden = false;
    state = null;
  }
  if (!response.ok) {
    const message = typeof data.detail === "string" ? data.detail : "Please check the entered values.";
    const error = new Error(data.error || message);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function busy(button, fn) {
  if (button.disabled) return;
  button.disabled = true;
  try { await fn(); } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function date(timestamp) {
  return timestamp ? new Date(timestamp * 1000).toLocaleString(undefined, {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "Not checked yet";
}

function sources(evidence) {
  return `<div class="source-links">${evidence.map((source) => `<a href="${escapeHTML(source.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHTML(source.detail)}">${escapeHTML(source.title)} ↗</a>`).join("")}</div>`;
}

function researchSteps(activity = []) {
  return activity.length ? `<details class="research-steps"><summary>Research activity · ${activity.length} steps</summary><ol>${activity.map((step) => `<li>${step.url ? `Opened <a href="${escapeHTML(step.url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(step.url)}</a>` : "Used live web research"}</li>`).join("")}</ol></details>` : "";
}

function renderWatches() {
  const watches = state.watches;
  $("#nav-count").textContent = watches.filter((w) => w.status === "active").length;
  $("#watch-total").textContent = String(watches.length).padStart(2,"0");
  $("#empty-state").hidden = watches.length > 0;
  const visible = watches.filter((w) => currentFilter === "all" || w.status === currentFilter);
  $("#watchlist").innerHTML = visible.map((watch) => {
    const status = watch.checking ? "Checking" : watch.status === "matched" ? "Found" : watch.status === "paused" ? "Paused" : watch.error ? "Needs attention" : watch.model?.id ? "Watching" : "Awaiting setup";
    const type = watch.status === "active" && watch.error ? "error" : watch.status;
    const next = watch.status === "active" ? (watch.checking ? "Checking now" : watch.next_check < Date.now()/1000 ? "Queued for check" : "Next " + date(watch.next_check)) : "Checked " + date(watch.last_check);
    return `<article class="watch-card" data-id="${watch.id}"><div class="watch-card-top"><h3>${escapeHTML(watch.title)}</h3><span class="status ${type}">${status}</span></div><p class="watch-goal">${escapeHTML(watch.goal)}</p>${watch.summary ? `<p class="watch-summary">${escapeHTML(watch.summary)}</p>` : ""}<div class="watch-meta"><div><span>${escapeHTML(watch.model?.id || "Awaiting model")}</span><span>${escapeHTML(intervals[watch.interval] || "daily")}${watch.automatic ? " · automatic" : ""}</span><span>${escapeHTML(watch.channels.map((channel) => channelNames[channel]).join(", ") || "In-app inbox")}</span><span>${escapeHTML(next)}</span></div><div class="watch-actions"><button class="text-button" data-action="history">History</button><button class="text-button" data-action="edit">Edit</button><button class="text-button" data-action="${watch.status === "active" ? "pause" : "resume"}">${watch.status === "active" ? "Pause" : watch.status === "matched" ? "Rearm" : "Resume"}</button>${watch.status === "active" ? '<button class="text-button" data-action="check">Check now</button>' : ""}<button class="text-button danger" data-action="delete">Delete</button></div></div></article>`;
  }).join("") || (watches.length ? '<p class="filtered-empty">No watches in this view.</p>' : "");
}

function renderInbox() {
  const notifications = state.inbox.notifications;
  const unseen = notifications.filter((n) => !n.seen).length;
  $("#inbox-count").textContent = unseen;
  $("#inbox-count").hidden = !unseen;
  $("#inbox-list").innerHTML = notifications.map((notification) => {
    const deliveries = state.inbox.deliveries.filter((d) => d.notification_id === notification.id);
    return `<article class="notification ${notification.seen ? "" : "unread"}"><h2>${escapeHTML(notification.title)}</h2><p>${escapeHTML(notification.body)}</p>${sources(notification.evidence)}<p class="small muted">Found ${escapeHTML(date(notification.created))}</p>${deliveries.map((d) => `<div class="delivery-state ${d.error ? "failed" : ""}">${channelNames[d.channel]}: ${d.delivered ? "delivered " + escapeHTML(date(d.delivered)) : d.error ? "delivery failed · retry scheduled" : "queued"}</div>`).join("")}</article>`;
  }).join("") || '<div class="empty-inbox"><h2>Nothing to interrupt you for. Yet.</h2><p>When a watch finds what you asked for, its evidence will appear here.</p></div>';
}

function renderSettings() {
  const settings = state.settings;
  $("#email").value = settings.email;
  $("#ntfy-topic").value = settings.ntfy_topic;
  $("#telegram-chat").value = settings.telegram_chat;
  $("#telegram-token").placeholder = settings.telegram_token_set ? "Token saved · leave empty to keep it" : "Token from BotFather";
  $("#codex-status").textContent = settings.codex_connected ? "Local ChatGPT sign-in connected. Research runs through the Codex agent runtime." : "Install Codex CLI and sign in on this server, then connect here.";
  $("#openrouter-status").textContent = settings.openrouter_connected ? "An OpenRouter key is saved securely on this server." : "No OpenRouter key saved yet.";
  $("#claude-status").textContent = settings.claude_connected ? "A Claude API key is saved securely on this server." : "No Claude API key saved yet.";
  $("#smtp-status").textContent = settings.smtp_ready ? "SMTP is configured on this server. Enable email on each watch you want delivered." : "The server needs SMTP_HOST and SMTP_FROM before email can be enabled. See the README.";
  $("#ntfy-token").placeholder = settings.ntfy_token_set ? "A token is saved · leave empty to keep it" : "Optional access token";
  $("#chatgpt-loopback").textContent = settings.chatgpt_loopback ? "Loopback callback is ready. A remote server can be reached using an SSH tunnel." : "ChatGPT sign-in requires the app’s 127.0.0.1 origin. Use a local instance or the SSH tunnel setup in the README.";
  $("#chatgpt-account").innerHTML = '<option value="">Add a new connection</option>' + settings.chatgpt_accounts.map((a) => `<option value="${escapeHTML(a.id)}">${escapeHTML(a.label)}${a.connected ? "" : " · reconnect"}</option>`).join("");
  $("#chatgpt-account").value = settings.chatgpt_selected || "";
  if (settings.model.provider) $("#provider").value = settings.model.provider;
  else if (settings.codex_connected) $("#provider").value = "codex";
  $("#model").value = settings.model.id || "";
  $("#current-model").textContent = settings.model.id ? `Current: ${settings.model.provider} / ${settings.model.id}` : "No model selected. Watches can be saved, and will begin after you choose one.";
  showProvider();
}

function showProvider() {
  $$(".provider-panel").forEach((panel) => { panel.hidden = panel.dataset.provider !== $("#provider").value; });
}

function renderStatus() {
  const worker = state.worker;
  $("#worker-label").textContent = worker.error ? "Worker needs attention" : worker.running ? "Worker online" : "Worker offline";
  $("#worker-dot").classList.toggle("offline", !worker.running || !!worker.error);
  $("#model-caption").textContent = state.settings.model.id ? state.settings.model.id : "Choose a model in Settings";
  const banner = $("#connection-banner");
  banner.replaceChildren();
  const connected = state.settings.codex_connected || state.settings.openrouter_connected || state.settings.claude_connected || state.settings.chatgpt_selected || state.settings.gemini_available;
  const issue = worker.error || (!worker.running ? "The background worker is offline. Checks resume when the server is running." : !connected ? "Connect an AI account in Settings to start checking. " : "");
  banner.hidden = !issue;
  if (issue) {
    banner.append(document.createTextNode(issue + " "));
    const button = document.createElement("button");
    button.textContent = "Open Settings ↗";
    button.addEventListener("click", () => navigate("settings"));
    banner.append(button);
  }
  updateLens();
}

async function refresh(initial = false) {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
    serverOffline = false;
    $("#login-view").hidden = true;
    $("#app").hidden = false;
    renderWatches(); renderInbox(); renderStatus();
    if (initial) { renderSettings(); renderWatchModels(); void loadWatchModels(); }
    updateDeliveryHint();
    return true;
  } catch (error) {
    if (error.status !== 401) { serverOffline = true; updateLens(); toast(error.message, true); $("#worker-label").textContent = "Server connection lost"; $("#worker-dot").classList.add("offline"); }
    return false;
  } finally { refreshing = false; }
}

function navigate(view) {
  currentView = view;
  ["watches","saved","inbox","settings"].forEach((name) => { $(`#view-${name}`).hidden = name !== view; });
  $$("[data-view]").forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle("selected", selected);
    if (selected) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");
  });
  $("#breadcrumb").textContent = {watches:"New watch",saved:"Watches",inbox:"Inbox",settings:"Connections"}[view];
  if (view === "settings") renderSettings();
  window.scrollTo({top:0,behavior:"instant"});
}

function resetWatch() {
  editing = null;
  $("#watch-form").reset();
  $("#cancel-edit").hidden = true;
  $("#save-watch").innerHTML = 'Set watch <span aria-hidden="true">↗</span>';
  lensError = "";
  renderWatchModels();
  updateDeliveryHint();
}

function editWatch(watch) {
  editing = watch.id;
  navigate("watches");
  $("#goal").value = watch.goal;
  $("#interval").value = watch.automatic ? "auto" : watch.interval;
  const channels = watch.channels.join(",");
  if (![...$("#notification").options].some((option) => option.value === channels)) $("#notification").add(new Option(watch.channels.map((channel) => channelNames[channel]).join(" + "), channels));
  $("#notification").value = channels;
  renderWatchModels(modelKey(watch.model));
  updateDeliveryHint();
  $("#cancel-edit").hidden = false;
  $("#save-watch").textContent = "Save & restart watch ↗";
  $("#goal").focus();
}

async function saveSettings() {
  const values = {email:$("#email").value.trim(),ntfy_topic:$("#ntfy-topic").value.trim(),telegram_chat:$("#telegram-chat").value.trim()};
  for (const [field,key] of [["#openrouter-key","openrouter_key"],["#claude-key","claude_key"],["#ntfy-token","ntfy_token"],["#telegram-token","telegram_token"]]) {
    if ($(field).value) values[key] = $(field).value.trim();
  }
  const saved = await api("/api/settings", "PUT", values);
  state.settings = saved;
  ["#openrouter-key","#claude-key","#ntfy-token","#telegram-token"].forEach((id) => { $(id).value = ""; });
  return saved;
}

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => { await api("/api/login","POST",{password:$("#password").value}); $("#password").value = ""; await refresh(true); });
});
$$("[data-lock]").forEach((button) => button.addEventListener("click", () => busy(button, async () => { await api("/api/logout","POST",{}); state = null; $("#app").hidden = true; $("#login-view").hidden = false; $("#password").focus(); })));
$$("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
$$("[data-filter]").forEach((button) => button.addEventListener("click", () => {
  currentFilter = button.dataset.filter;
  $$("[data-filter]").forEach((item) => { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); });
  renderWatches();
}));
$("#cancel-edit").addEventListener("click", resetWatch);
$("#watch-model").addEventListener("change", updateLens);
$("#notification").addEventListener("change", updateDeliveryHint);
$("#configure-delivery").addEventListener("click", () => navigate("settings"));
$("#goal").addEventListener("focus", () => { listening = true; updateLens(); });
$("#goal").addEventListener("blur", () => { listening = false; updateLens(); });
$("#goal").addEventListener("input", () => {
  listening = true;
  lensError = "";
  $("#mascot").dataset.typing = "true";
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => { $("#mascot").dataset.typing = "false"; }, 400);
  updateLens();
});
$("#goal").addEventListener("keydown", (event) => {
  if (!submitsObjective(event)) return;
  event.preventDefault();
  if (!$("#save-watch").disabled) $("#watch-form").requestSubmit($("#save-watch"));
});
$("#interval").addEventListener("change", () => {
  $("#frequency-note").textContent = $("#interval").value === "60" ? "Every-minute checks can use 1,440 model and search requests per day, per watch. Checks run sequentially; a slow search may delay the next one." : "Automatic checks usually start daily. Frequent checks use more model and search allowance.";
  $("#frequency-note").hidden = $("#interval").value !== "60";
});
$("#watch-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    if (/\[[^\]]+\]/.test($("#goal").value) && !confirm("This goal still contains square-bracket placeholders. Save it anyway? The model may need more details.")) return;
    const body = {goal:$("#goal").value.trim(), interval:$("#interval").value === "auto" ? null : Number($("#interval").value),
      channels:$("#notification").value.split(",").filter(Boolean),model:$("#watch-model").value ? JSON.parse($("#watch-model").value) : null};
    watchSubmitting = true;
    listening = false;
    lensError = "";
    clearTimeout(typingTimer);
    $("#mascot").dataset.typing = "false";
    updateLens();
    try {
      const result = await api(editing ? `/api/watches/${editing}` : "/api/watches", editing ? "PUT" : "POST", body);
      const updated = !!editing;
      lastSavedId = editing || result.id;
      resetWatch();
      await refresh();
      toast(updated ? "Objective revised. A fresh check is queued." : "Objective secured. I’ll take it from here.");
    } catch (error) {
      lensError = "Your objective could not be saved. Your text is still here; please try again.";
      throw error;
    } finally {
      watchSubmitting = false;
      updateLens();
    }
  });
});
$("#watchlist").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const watch = state.watches.find((w) => w.id === button.closest("[data-id]").dataset.id);
  const action = button.dataset.action;
  if (action === "edit") return editWatch(watch);
  busy(button, async () => {
    if (action === "history") {
      const history = await api(`/api/watches/${watch.id}/history`);
      $("#history-title").textContent = watch.title;
      $("#history-content").innerHTML = (watch.approach ? `<div class="approach-note">${escapeHTML(watch.approach)}</div>` : "") + history.checks.map((check) => `<article class="history-item"><div><span class="status ${check.outcome === "matched" ? "matched" : check.outcome === "inconclusive" ? "error" : ""}">${escapeHTML(check.outcome)}</span><span class="history-time">${escapeHTML(date(check.checked))} · ${escapeHTML(check.model)}</span></div><p>${escapeHTML(check.summary)}</p>${sources(check.evidence)}${researchSteps(check.activity)}</article>`).join("") + (!history.checks.length ? '<p class="muted">No checks yet. Choose a model to start.</p>' : "");
      $("#history-dialog").showModal();
      return;
    }
    if (action === "delete" && !confirm("Delete this watch, its history and its notifications? This cannot be undone.")) return;
    await api(`/api/watches/${watch.id}/${action}`,"POST",{});
    await refresh();
    if (editing === watch.id && action === "delete") resetWatch();
    toast(action === "check" ? "A fresh check is queued." : action === "delete" ? "Watch deleted." : action === "pause" ? "Watch paused." : "Watch rearmed. A fresh check is queued.");
  });
});
$("#close-history").addEventListener("click", () => $("#history-dialog").close());
$("#mark-read").addEventListener("click", () => busy($("#mark-read"), async () => { await api("/api/inbox/read","POST",{}); await refresh(); }));
$("#provider").addEventListener("change", () => { showProvider(); $("#model-list").replaceChildren(); $("#model").value = ""; });
$("#settings-form").addEventListener("submit", (event) => { event.preventDefault(); busy(event.submitter, async () => { await saveSettings(); await refresh(); await loadWatchModels(); toast("Connections saved."); }); });
$("#connect-codex").addEventListener("click", () => busy($("#connect-codex"), async () => { await api("/api/codex/connect","POST",{}); await refresh(); renderSettings(); await loadWatchModels(); toast("Local ChatGPT sign-in connected."); }));
$("#load-models").addEventListener("click", () => busy($("#load-models"), async () => {
  await saveSettings();
  const result = await api(`/api/models/${$("#provider").value}`);
  watchCatalog = watchCatalog.filter((model) => model.provider !== $("#provider").value).concat(result.models.map((model) => ({provider:$("#provider").value,...model})));
  renderWatchModels();
  $("#model-list").innerHTML = result.models.map((model) => `<option value="${escapeHTML(model.id)}">${escapeHTML(model.name)}</option>`).join("");
  if (result.models.length === 1) $("#model").value = result.models[0].id;
  $("#model-note").textContent = `${result.models.length} models loaded. Choose a model ID below.${$("#provider").value === "gemini_cli" ? " You may also enter a model ID supported by your CLI." : ""}`;
  toast("Model catalog loaded."); $("#model").focus();
}));
$("#model-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => { await saveSettings(); await api("/api/model","PUT",{provider:$("#provider").value,id:$("#model").value.trim()}); await refresh(); renderSettings(); toast("Model selected. Active watches can now begin checking."); });
});
$$(".oauth-button").forEach((button) => button.addEventListener("click", () => busy(button, async () => {
  const result = await api(`/api/oauth/${button.dataset.oauth}/start`,"POST",{account_id:button.dataset.oauth === "chatgpt" ? $("#chatgpt-account").value || null : null});
  location.assign(result.url);
})));
$("#choose-account").addEventListener("click", () => busy($("#choose-account"), async () => {
  if (!$("#chatgpt-account").value) throw new Error("Choose a saved ChatGPT connection first.");
  await api("/api/chatgpt/account","PUT",{account_id:$("#chatgpt-account").value}); await refresh(); renderSettings(); toast("Connection selected. Load models for this account.");
}));
$("#disconnect-account").addEventListener("click", () => busy($("#disconnect-account"), async () => {
  if (!$("#chatgpt-account").value) throw new Error("Choose a saved ChatGPT connection first.");
  if (!confirm("Revoke this ChatGPT session? Checks using it will stop until another account and model are selected.")) return;
  await api("/api/chatgpt/disconnect","POST",{account_id:$("#chatgpt-account").value}); await refresh(); renderSettings(); toast("ChatGPT session revoked and disconnected.");
}));

(async () => {
  const query = new URLSearchParams(location.search);
  history.replaceState({},"",location.pathname);
  const unlocked = await refresh(true);
  if (unlocked && (query.has("connected") || query.has("connection_error"))) {
    navigate("settings");
    if (query.has("connection_error")) toast((await api("/api/connection-error")).error, true);
    else toast("Account connected. Load its models and choose one to begin.");
  }
})();
setInterval(() => { if (state && !document.hidden) refresh(); }, 10000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && state) refresh(); });
