"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const intervals = {60:"every minute",300:"every 5 minutes",900:"every 15 minutes",3600:"hourly",21600:"every 6 hours",86400:"daily",604800:"weekly",2592000:"every 30 days"};
const examples = {
  stock:"Let me know when [exact product and variant] is back in stock at [retailer URL], available for delivery to [country], for no more than [price and currency].",
  research:"Let me know when a new treatment for [condition] is approved by [regulator] in [country]. Distinguish clinical trial results from regulatory approval, and use official sources.",
  games:"Let me know when a new online co-op game releases on PC with an OpenCritic score of at least 85 from at least 10 critic reviews. It must support two players and have a full release, not early access.",
};
let state = null;
let currentView = "watches";
let currentFilter = "all";
let editing = null;
let refreshing = false;
let toastTimer;

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

function renderWatches() {
  const watches = state.watches;
  $("#nav-count").textContent = watches.filter((w) => w.status === "active").length;
  $("#watch-total").textContent = String(watches.length).padStart(2,"0");
  $("#empty-state").hidden = watches.length > 0;
  $("#examples").hidden = watches.length > 0;
  const visible = watches.filter((w) => currentFilter === "all" || w.status === currentFilter);
  $("#watchlist").innerHTML = visible.map((watch) => {
    const status = watch.checking ? "Checking" : watch.status === "matched" ? "Found" : watch.status === "paused" ? "Paused" : watch.error ? "Needs attention" : state.settings.model.id ? "Watching" : "Awaiting setup";
    const type = watch.status === "active" && watch.error ? "error" : watch.status;
    const next = watch.status === "active" ? (watch.checking ? "Checking now" : watch.next_check < Date.now()/1000 ? "Queued for check" : "Next " + date(watch.next_check)) : "Checked " + date(watch.last_check);
    return `<article class="watch-card" data-id="${watch.id}"><div class="watch-card-top"><h3>${escapeHTML(watch.title)}</h3><span class="status ${type}">${status}</span></div><p class="watch-goal">${escapeHTML(watch.goal)}</p>${watch.summary ? `<p class="watch-summary">${escapeHTML(watch.summary)}</p>` : ""}<div class="watch-meta"><div><span>◷ ${escapeHTML(intervals[watch.interval] || "daily")}${watch.automatic ? " · automatic" : ""}</span><span>${escapeHTML(next)}</span></div><div class="watch-actions"><button class="text-button" data-action="history">History</button><button class="text-button" data-action="edit">Edit</button><button class="text-button" data-action="${watch.status === "active" ? "pause" : "resume"}">${watch.status === "active" ? "Pause" : watch.status === "matched" ? "Rearm" : "Resume"}</button>${watch.status === "active" ? '<button class="text-button" data-action="check">Check now</button>' : ""}<button class="text-button danger" data-action="delete">Delete</button></div></div></article>`;
  }).join("") || (watches.length ? '<p class="filtered-empty">No watches in this view.</p>' : "");
}

function renderInbox() {
  const notifications = state.inbox.notifications;
  const unseen = notifications.filter((n) => !n.seen).length;
  $("#inbox-count").textContent = unseen;
  $("#inbox-count").hidden = !unseen;
  $("#inbox-list").innerHTML = notifications.map((notification) => {
    const deliveries = state.inbox.deliveries.filter((d) => d.notification_id === notification.id);
    return `<article class="notification ${notification.seen ? "" : "unread"}"><h2>${escapeHTML(notification.title)}</h2><p>${escapeHTML(notification.body)}</p>${sources(notification.evidence)}<p class="small muted">Found ${escapeHTML(date(notification.created))}</p>${deliveries.map((d) => `<div class="delivery-state ${d.error ? "failed" : ""}">${d.channel === "email" ? "Email" : "Push"}: ${d.delivered ? "delivered " + escapeHTML(date(d.delivered)) : d.error ? "delivery failed · retry scheduled" : "queued"}</div>`).join("")}</article>`;
  }).join("") || '<div class="empty-inbox"><h2>Nothing to interrupt you for. Yet.</h2><p>When a watch finds what you asked for, its evidence will appear here.</p></div>';
}

function renderSettings() {
  const settings = state.settings;
  $("#email").value = settings.email;
  $("#ntfy-topic").value = settings.ntfy_topic;
  $("#openrouter-status").textContent = settings.openrouter_connected ? "An OpenRouter key is saved securely on this server." : "No OpenRouter key saved yet.";
  $("#claude-status").textContent = settings.claude_connected ? "A Claude API key is saved securely on this server." : "No Claude API key saved yet.";
  $("#smtp-status").textContent = settings.smtp_ready ? "SMTP is configured on this server. Enable email on each watch you want delivered." : "The server needs SMTP_HOST and SMTP_FROM before email can be enabled. See the README.";
  $("#ntfy-token").placeholder = settings.ntfy_token_set ? "A token is saved · leave empty to keep it" : "Optional access token";
  $("#chatgpt-loopback").textContent = settings.chatgpt_loopback ? "Loopback callback is ready. A remote server can be reached using an SSH tunnel." : "ChatGPT sign-in requires the app’s 127.0.0.1 origin. Use a local instance or the SSH tunnel setup in the README.";
  $("#chatgpt-account").innerHTML = '<option value="">Add a new connection</option>' + settings.chatgpt_accounts.map((a) => `<option value="${escapeHTML(a.id)}">${escapeHTML(a.label)}${a.connected ? "" : " · reconnect"}</option>`).join("");
  $("#chatgpt-account").value = settings.chatgpt_selected || "";
  if (settings.model.provider) $("#provider").value = settings.model.provider;
  $("#model").value = settings.model.id || "";
  $("#current-model").textContent = settings.model.id ? `Current: ${settings.model.provider} / ${settings.model.id}` : "No model selected. Watches can be saved, and will begin after you choose one.";
  showProvider();
}

function showProvider() {
  $$(".provider-panel").forEach((panel) => { panel.hidden = panel.dataset.provider !== $("#provider").value; });
}

function renderStatus() {
  const worker = state.worker;
  $("#worker-label").textContent = worker.error ? "Worker needs attention" : worker.running ? "Background worker running" : "Background worker offline";
  $("#worker-dot").classList.toggle("offline", !worker.running || !!worker.error);
  $("#model-caption").textContent = state.settings.model.id ? state.settings.model.id : "Choose a model in Settings";
  const banner = $("#connection-banner");
  banner.replaceChildren();
  const issue = worker.error || (!worker.running ? "The background worker is offline. Checks resume when the server is running." : !state.settings.model.id ? "Your watchlist is ready. Connect an account and choose a model to begin checking. " : "");
  banner.hidden = !issue;
  if (issue) {
    banner.append(document.createTextNode(issue + " "));
    const button = document.createElement("button");
    button.textContent = "Open Settings ↗";
    button.addEventListener("click", () => navigate("settings"));
    banner.append(button);
  }
}

async function refresh(initial = false) {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
    $("#login-view").hidden = true;
    $("#app").hidden = false;
    renderWatches(); renderInbox(); renderStatus();
    if (initial) renderSettings();
    return true;
  } catch (error) {
    if (error.status !== 401) { toast(error.message, true); $("#worker-label").textContent = "Server connection lost"; $("#worker-dot").classList.add("offline"); }
    return false;
  } finally { refreshing = false; }
}

function navigate(view) {
  currentView = view;
  ["watches","inbox","settings"].forEach((name) => { $(`#view-${name}`).hidden = name !== view; });
  $$("[data-view]").forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle("selected", selected);
    if (selected) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");
  });
  $("#breadcrumb").textContent = {watches:"Your personal lookout",inbox:"A moment worth remembering",settings:"Your own way of keeping watch"}[view];
  if (view === "settings") renderSettings();
  window.scrollTo({top:0,behavior:"instant"});
}

function resetWatch() {
  editing = null;
  $("#watch-form").reset();
  $("#cancel-edit").hidden = true;
  $("#save-watch").innerHTML = 'Keep an eye on it <span aria-hidden="true">↗</span>';
}

function editWatch(watch) {
  editing = watch.id;
  navigate("watches");
  $("#goal").value = watch.goal;
  $("#interval").value = watch.automatic ? "auto" : watch.interval;
  $("#channel-email").checked = watch.channels.includes("email");
  $("#channel-push").checked = watch.channels.includes("push");
  $("#advanced").open = true;
  $("#cancel-edit").hidden = false;
  $("#save-watch").textContent = "Save & restart watch ↗";
  $("#goal").focus();
}

async function saveSettings() {
  const values = {email:$("#email").value.trim(),ntfy_topic:$("#ntfy-topic").value.trim()};
  for (const [field,key] of [["#openrouter-key","openrouter_key"],["#claude-key","claude_key"],["#ntfy-token","ntfy_token"]]) {
    if ($(field).value) values[key] = $(field).value.trim();
  }
  const saved = await api("/api/settings", "PUT", values);
  state.settings = saved;
  ["#openrouter-key","#claude-key","#ntfy-token"].forEach((id) => { $(id).value = ""; });
  return saved;
}

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => { await api("/api/login","POST",{password:$("#password").value}); $("#password").value = ""; await refresh(true); });
});
$("#logout").addEventListener("click", () => busy($("#logout"), async () => { await api("/api/logout","POST",{}); state = null; $("#app").hidden = true; $("#login-view").hidden = false; $("#password").focus(); }));
$$("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
$$("[data-filter]").forEach((button) => button.addEventListener("click", () => {
  currentFilter = button.dataset.filter;
  $$("[data-filter]").forEach((item) => { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); });
  renderWatches();
}));
$$("[data-example]").forEach((button) => button.addEventListener("click", () => { resetWatch(); $("#goal").value = examples[button.dataset.example]; $("#goal").focus(); }));
$("#cancel-edit").addEventListener("click", resetWatch);
$("#interval").addEventListener("change", () => {
  $("#frequency-note").textContent = $("#interval").value === "60" ? "Every-minute checks can use 1,440 model and search requests per day, per watch. Checks run sequentially; a slow search may delay the next one." : "Automatic checks usually start daily. Frequent checks use more model and search allowance.";
});
$("#watch-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    if (/\[[^\]]+\]/.test($("#goal").value) && !confirm("This goal still contains square-bracket placeholders. Save it anyway? The model may need more details.")) return;
    const body = {goal:$("#goal").value.trim(), interval:$("#interval").value === "auto" ? null : Number($("#interval").value),
      channels:[...($("#channel-push").checked ? ["push"] : []),...($("#channel-email").checked ? ["email"] : [])]};
    await api(editing ? `/api/watches/${editing}` : "/api/watches", editing ? "PUT" : "POST", body);
    const updated = !!editing;
    resetWatch(); await refresh(); toast(updated ? "Watch updated. A fresh check is queued." : "Remembered. Your watch is safely on the list.");
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
      $("#history-content").innerHTML = (watch.approach ? `<div class="approach-note">${escapeHTML(watch.approach)}</div>` : "") + history.checks.map((check) => `<article class="history-item"><div><span class="status ${check.outcome === "matched" ? "matched" : check.outcome === "inconclusive" ? "error" : ""}">${escapeHTML(check.outcome)}</span><span class="history-time">${escapeHTML(date(check.checked))} · ${escapeHTML(check.model)}</span></div><p>${escapeHTML(check.summary)}</p>${sources(check.evidence)}</article>`).join("") + (!history.checks.length ? '<p class="muted">No checks yet. Choose a model in Settings to start.</p>' : "");
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
$("#settings-form").addEventListener("submit", (event) => { event.preventDefault(); busy(event.submitter, async () => { await saveSettings(); await refresh(); toast("Connections saved."); }); });
$("#load-models").addEventListener("click", () => busy($("#load-models"), async () => {
  await saveSettings();
  const result = await api(`/api/models/${$("#provider").value}`);
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

$("#today").textContent = new Date().toLocaleDateString(undefined,{day:"numeric",month:"long",year:"numeric"}).toUpperCase();
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
