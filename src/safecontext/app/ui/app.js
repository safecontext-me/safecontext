// SafeContext window. All mail, event and chat content is inserted with textContent /
// text nodes, never parsed as HTML, so nothing in an item can run code or load resources.
"use strict";

const REDACTION_NAMES = {
  ONE_TIME_CODE: "2FA code", TOTP_SECRET: "authenticator key", BACKUP_CODE: "backup code",
  CREDIT_CARD: "card number", CVV: "card code", SSN: "SSN", API_KEY: "API key", JWT: "login token",
  PRIVATE_KEY: "private key", PASSWORD: "password", IBAN: "IBAN", ROUTING_NUMBER: "routing no.",
  BANK_ACCOUNT: "account no.", DATE_OF_BIRTH: "birth date", PHONE: "phone", STREET_ADDRESS: "address",
  HEALTH: "health detail", LEGAL: "legal detail", PERSONAL: "personal detail",
};

const SOURCE_ICONS = { gmail: "✉️", gcal: "📅", imessage: "💬", whatsapp: "🟢", recordings: "🎙️" };
const OPEN_LABELS = { gmail: "Open in Gmail", gcal: "Open in Calendar" };
const STATE_LABELS = {
  connected: ["Connected", "ok"], syncing: ["Syncing…", "busy"], needs_reconnect: ["Needs reconnecting", "warn"],
  needs_permission: ["Needs permission", "warn"], error: ["Will retry", "warn"], not_connected: ["Not connected", ""],
  coming_soon: ["Coming soon", ""],
};
const REQ_KINDS = { oauth_scope: "Google permission", macos_permission: "Mac permission", file: "File", folder: "Folder" };

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
let state = { status: null, view: "mail", shown: false, mcp: null, mcpBusy: false };

// ---------- DOM helpers ----------
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

// Text with [REDACTED:TYPE] tokens rendered as chips, «» search highlights as <mark>,
// and (optionally) Gmail links made clickable. Everything else stays plain text.
function richText(text, { links = false } = {}) {
  const frag = document.createDocumentFragment();
  const re = /\[REDACTED:([A-Z_]+)\]|«([^»]*)»|(https:\/\/(?:mail\.google\.com\/mail\/|www\.google\.com\/calendar\/)[^\s)\]]+)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    frag.append(document.createTextNode(text.slice(last, m.index)));
    if (m[1]) {
      frag.append(el("span", { class: "redacted", title: "Removed on this Mac before saving" },
        REDACTION_NAMES[m[1]] || m[1].toLowerCase()));
    } else if (m[2] !== undefined) {
      frag.append(el("mark", {}, m[2]));
    } else if (links) {
      const url = m[3];
      frag.append(el("a", { onclick: () => openLink(url) }, url.includes("/calendar/") ? "Open in Calendar" : "Open in Gmail"));
    } else {
      frag.append(document.createTextNode(m[3]));
    }
    last = re.lastIndex;
  }
  frag.append(document.createTextNode(text.slice(last)));
  return frag;
}

function senderName(s) {
  const m = /^\s*"?([^"<]+?)"?\s*<[^>]+>\s*$/.exec(s || "");
  return m ? m[1] : (s || "Unknown sender");
}

function fmtDate(iso, withTime = false) {
  if (!iso) return "";
  const d = new Date(iso);
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  if (withTime) Object.assign(opts, { hour: "numeric", minute: "2-digit" });
  return d.toLocaleString(undefined, opts);
}

function fmtSize(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function ago(ms) {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ms).toLocaleDateString();
}

const n = (x) => Number(x || 0).toLocaleString();

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2600);
}

function confirmDialog(text, okLabel) {
  return new Promise((resolve) => {
    const d = $("confirm");
    $("confirm-text").textContent = text;
    $("confirm-ok").textContent = okLabel;
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
  });
}

async function openLink(url) {
  const r = await api().open_link(url);
  if (!r.ok) toast(r.error);
}

function sourceName(id) {
  const st = state.status && (state.status.sources || []).find((x) => x.id === id);
  return st ? st.name : id;
}

function fmtRange(start, end, allDay) {
  if (!start) return "";
  if (allDay) return fmtDate(start) + " · all day";
  const s = fmtDate(start, true);
  if (!end) return s;
  const sd = new Date(start), ed = new Date(end);
  const sameDay = sd.toDateString() === ed.toDateString();
  return `${s} – ${sameDay ? ed.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : fmtDate(end, true)}`;
}

function monthYear(ms) {
  return new Date(ms).toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

function duration(ms) {
  const m = Math.floor(ms / 60000), h = Math.floor(m / 60);
  return h ? `${h}h ${m % 60}m` : `${m}m`;
}

// ---------- status / shell ----------
async function refreshStatus() {
  const r = await api().status();
  if (!r.ok) return;
  state.status = r;
  $("loading").hidden = true;
  const connected = !!r.account || r.any_connected;
  $("onboarding").hidden = connected;
  $("shell").hidden = !connected;
  if (!connected) return;

  $("demo-badge").hidden = !r.demo;
  const accounts = [...new Set((r.sources || []).filter((x) => x.account).map((x) => x.account))];
  $("account").textContent = r.account || accounts.join(", ");
  if (state.view === "sources") renderSources(r.sources || []);
  $("banner").hidden = !r.needs_reconnect;
  $("banner-text").textContent = r.reconnect_message;

  const dot = $("sync-dot");
  const p = r.progress;
  const waiting = p && p.waiting_until_ms && p.waiting_until_ms > Date.now();
  dot.className = "dot" + (waiting ? " warn" : r.syncing ? " busy" : r.needs_reconnect || (r.last_result && r.last_result.status !== "ok") ? " warn" : "");
  $("sync-stats").hidden = !(r.syncing && p.backfill);
  $("sync-note").hidden = true;
  if (r.syncing) {
    if (waiting) {
      $("sync-text").textContent = "Paused for Gmail limit";
      $("sync-sub").textContent = `Resuming in ${Math.max(1, Math.round((p.waiting_until_ms - Date.now()) / 1000))}s`;
    } else {
      $("sync-text").textContent = p.backfill ? "Syncing…" : "Checking for new mail…";
      $("sync-sub").textContent = p.backfill ? "Newest mail first" : `${n(p.kept)} kept · ${n(p.dropped)} skipped`;
    }
    if (p.backfill) {
      $("ss-done").textContent = n(r.kept + r.dropped);
      $("ss-kept").textContent = n(r.kept);
      $("ss-reach").textContent = r.oldest_ms ? monthYear(r.oldest_ms) : "—";
      $("ss-pace").textContent = p.per_min == null ? "measuring…" : `${n(Math.round(p.per_min))}/min`;
      $("ss-time").textContent = p.started_ms ? duration(Date.now() - p.started_ms) : "—";
    }
    if (p.elsewhere) {
      $("sync-note").hidden = false;
      $("sync-note").textContent = "Running from the command line. It pauses while your Mac sleeps.";
    }
  } else if (!r.account) {
    const others = (r.sources || []).filter((x) => x.available && x.account);
    const busy = others.some((x) => x.syncing);
    dot.className = "dot" + (busy ? " busy" : others.some((x) => ["error", "needs_reconnect", "needs_permission"].includes(x.state)) ? " warn" : "");
    $("sync-text").textContent = busy ? "Syncing…" : "Up to date";
    $("sync-sub").textContent = `${n(r.total_items)} items · ${others.map((x) => x.name).join(", ")}`;
  } else if (r.needs_reconnect) {
    $("sync-text").textContent = "Paused";
    $("sync-sub").textContent = "Gmail needs reconnecting";
  } else if (r.last_result && r.last_result.status !== "ok") {
    $("sync-text").textContent = "Will retry";
    $("sync-sub").textContent = r.last_result.message;
  } else if (!r.first_sync_done) {
    $("sync-text").textContent = "Paused";
    $("sync-sub").textContent = `${n(r.kept)} messages so far${r.oldest_ms ? " · back to " + monthYear(r.oldest_ms) : ""}. Sync now to continue.`;
  } else {
    $("sync-text").textContent = "Up to date";
    const extra = r.total_items > r.kept ? ` · ${n(r.total_items - r.kept)} other items` : "";
    $("sync-sub").textContent = `${n(r.kept)} messages${extra} · ${r.last_sync_ms ? ago(r.last_sync_ms) : "never synced"}`;
  }
  const anySyncing = (r.sources || []).some((x) => x.syncing);
  $("sync-now").disabled = anySyncing || (!!r.account && r.needs_reconnect && !(r.sources || []).some((x) => x.id !== "gmail" && x.account));

  $("s-account").textContent = r.account || "Not connected";
  $("s-mode").textContent = r.mode === "headers" ? "Headers only (no message text)" : "Full messages, scrubbed";
  $("s-dir").textContent = r.data_dir;

  if (!state.shown) { state.shown = true; show(state.view); }  // first time the window has a screen to show
}

function show(view) {
  state.view = view;
  for (const b of document.querySelectorAll(".nav button")) b.classList.toggle("active", b.dataset.view === view);
  for (const v of ["mail", "sources", "mcp", "removed", "settings"]) $(`view-${v}`).hidden = v !== view;
  if (view === "mail") mailShow();
  if (view === "sources") renderSources((state.status && state.status.sources) || []);
  if (view === "removed") loadRemoved();
  if (view === "mcp") loadMcp();
}

// ---------- reader cards (used by mail.js) ----------
function openButton(m) {
  if (!m.link) return null;
  return el("button", { class: "btn small", onclick: () => openLink(m.link) }, OPEN_LABELS[m.source] || "Open original");
}

function messageCard(m) {
  const atts = (m.attachments || []).map((a) =>
    el("span", { class: "att", title: "Attachment contents are never downloaded" },
      "📎 ", richText(a.filename), " · ", a.mime_type, a.size ? ` · ${fmtSize(a.size)}` : ""));
  const to = m.recipients || m.participants;
  return el("article", { class: "msg" },
    el("div", { class: "msg-head" },
      el("div", {},
        el("div", { class: "msg-from" }, m.sender),
        el("div", { class: "msg-meta" }, fmtDate(m.date, true), to ? ` · to ${to}` : "")),
      openButton(m)),
    el("div", { class: "msg-body" }, m.body ? richText(m.body) : el("span", { class: "muted" },
      m.source === "gmail" && state.status && state.status.mode === "headers" ? "Headers-only mode: no message text is stored." : "(empty)")),
    m.body_truncated ? el("p", { class: "muted" }, "Long item shortened. Open the original for the rest.") : null,
    atts.length ? el("div", { class: "atts" }, atts) : null);
}

function eventCard(m) {
  const d = Object.assign({}, m.details || {});
  const allDay = d.When === "All day";
  delete d.When;
  const rows = [["When", fmtRange(m.date, m.end, allDay)]];
  if (m.sender) rows.push(["Organizer", m.sender]);
  if (m.participants) rows.push(["Guests", m.participants]);
  for (const [k, v] of Object.entries(d)) rows.push([k, v]);
  return el("article", { class: "msg" },
    el("div", { class: "msg-head" },
      el("div", { class: "msg-meta" }, "📅 ", sourceName(m.source)),
      openButton(m)),
    el("dl", { class: "details" }, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, richText(String(v)))])),
    m.body ? el("div", { class: "msg-body" }, richText(m.body)) : null,
    m.body_truncated ? el("p", { class: "muted" }, "Long description shortened. Open the original for the rest.") : null);
}

// ---------- sources ----------
function renderSources(list) {
  const grid = $("source-grid");
  if (state.sourcesBusy) return;  // don't redraw under a pending click
  grid.replaceChildren(...list.map(sourceCard));
}

function sourceCard(x) {
  let [label, tone] = STATE_LABELS[x.state] || [x.state, ""];
  const perm = x.state === "needs_permission" ? x.permission : null;
  if (perm) label = `Needs ${perm.title}`;
  const soon = x.state === "coming_soon";
  const connected = !!x.account && !soon;
  const card = el("div", { class: "card src-card" + (soon ? " soon" : "") },
    el("div", { class: "src-head" },
      el("div", { class: "src-name" }, el("span", { class: "src-icon", "aria-hidden": "true" }, SOURCE_ICONS[x.id] || "•"), x.name),
      el("span", { class: `pill ${tone}` }, label)),
    el("p", { class: "src-desc" }, x.description),
    x.message && !perm ? el("div", { class: "src-msg" }, x.message) : null,
    perm ? permissionBox(x, perm) : null,
    perm ? null : el("div", { class: "needs-k" }, "Access it needs"),
    perm ? null : el("ul", { class: "needs" }, (x.requirements || []).map((q) =>
      el("li", {}, el("b", {}, q.label), el("span", { class: "muted" }, REQ_KINDS[q.kind] || ""), el("span", { class: "why" }, q.why)))));
  if (connected) {
    card.append(el("div", { class: "src-stats" },
      el("div", {}, el("b", {}, n(x.item_count)), el("span", {}, "kept")),
      el("div", {}, el("b", {}, n(x.dropped_count)), el("span", {}, "skipped")),
      el("div", {}, el("b", {}, x.syncing ? "now" : x.last_sync_ms ? ago(x.last_sync_ms) : "—"), el("span", {}, "last sync"))),
      el("div", { class: "muted", style: "font-size:12px" }, x.account));
  }
  const actions = el("div", { class: "actions" });
  if (soon) {
    actions.append(el("button", { class: "btn", disabled: "" }, "Coming soon"));
  } else if (perm) {
    // The permission box carries Open System Settings; here: try again, or back out.
    actions.append(el("button", { class: "btn", onclick: (e) => connectSource(x, e.target) }, "Check again"));
    if (connected) actions.append(el("button", { class: "btn danger", onclick: () => disconnectSource(x) }, "Disconnect"));
    else actions.append(el("button", { class: "btn", onclick: () => cancelSource(x) }, "Not now"));
  } else if (!connected) {
    actions.append(el("button", { class: "btn primary", onclick: (e) => connectSource(x, e.target) }, `Connect ${x.name}`));
  } else {
    if (x.state === "needs_reconnect" || x.state === "needs_permission")
      actions.append(el("button", { class: "btn primary", onclick: (e) => connectSource(x, e.target) }, "Reconnect"));
    const sync = el("button", { class: "btn", onclick: async () => { await api().sync_source(x.id); refreshStatus(); } }, "Sync now");
    if (x.syncing || x.state === "needs_reconnect" || x.state === "needs_permission") sync.disabled = true;
    actions.append(sync, el("button", { class: "btn danger", onclick: () => disconnectSource(x) }, "Disconnect"));
  }
  card.append(actions);
  return card;
}

// A missing macOS permission (Full Disk Access): what it is, why, the steps, and a button
// that opens the right System Settings pane. Nothing is requested until the user clicks.
function permissionBox(x, perm) {
  return el("div", { class: "perm" },
    el("div", { class: "perm-title" }, x.message || `SafeContext needs ${perm.title}.`),
    el("p", {}, perm.explain),
    el("ol", {}, perm.steps.map((step) => el("li", {}, step))),
    el("button", { class: "btn primary", onclick: (e) => openSourceSettings(x, e.target) }, perm.button));
}

async function openSourceSettings(x, btn) {
  btn.disabled = true;
  const r = await api().open_source_settings(x.id);
  btn.disabled = false;
  if (!r.ok) toast(r.error);
}

async function connectSource(x, btn) {
  state.sourcesBusy = true;
  btn.disabled = true;
  btn.textContent = x.requirements.some((q) => q.kind === "oauth_scope") ? "Waiting for Google sign-in…"
    : x.requirements.some((q) => q.kind === "macos_permission") ? "Checking access…" : "Connecting…";
  const opts = x.id === "gmail" ? { mode: (state.status && state.status.mode) || "full" } : {};
  const r = await api().connect_source(x.id, opts);
  state.sourcesBusy = false;
  // A missing permission isn't an error to dismiss: the card now shows what to do.
  if (r.ok) toast(`Connected ${x.name}`);
  else if (r.needs_permission) toast(`${x.name} needs ${x.requirements.find((q) => q.value === r.requirement)?.label || "a Mac permission"}. See the ${x.name} card.`);
  else toast(r.error);
  refreshStatus();
}

async function cancelSource(x) {
  const r = await api().disconnect_source(x.id, true);  // nothing was synced yet; this only forgets the attempt
  if (!r.ok) toast(r.error);
  refreshStatus();
}

async function disconnectSource(x) {
  const ok = await confirmDialog(
    `Disconnect ${x.name} and delete its ${n(x.item_count)} scrubbed items from this Mac? This can't be undone.`, "Disconnect");
  if (!ok) return;
  const r = await api().disconnect_source(x.id, true);
  toast(r.ok ? `${x.name} disconnected and its data deleted.` : r.error);
  if (x.id === "gmail") state.status = null;
  refreshStatus();
}

// ---------- MCP (relay link for Grok, Claude, Muse) ----------
// The backend calls keep their grok_* names: they drive the same relay link for every app.
const MCP_STATES = {
  off: ["Off", ""], connecting: ["Connecting…", "busy"], waiting: ["Waiting for an AI app to connect", "busy"],
  connected: ["Connected", "ok"], offline: ["Can't reach the relay. Retrying…", "warn"],
  rejected: ["Link not accepted. Regenerate it.", "warn"],
};
const MCP_APPS = ["grok", "claude", "muse"];

async function refreshMcp() {
  const r = await api().grok_status();
  if (r.ok) renderMcp(r);
}

function renderMcp(g) {
  state.mcp = g;
  const on = g.enabled;
  if (!state.mcpBusy) $("mcp-toggle").checked = on;
  const [label, tone] = MCP_STATES[g.state] || [g.state, ""];
  let text = label;
  if (g.state === "connected" && g.last_used_ms) text += ` · last used ${ago(g.last_used_ms)}`;
  $("mcp-status-text").textContent = text;
  $("mcp-dot").className = "dot" + (tone === "ok" ? " on" : tone === "busy" ? " busy" : tone === "warn" ? " warn" : " off");
  $("mcp-card").classList.toggle("is-on", on);
  $("mcp-on").hidden = !on;
  $("mcp-pitch").hidden = on;
  $("mcp-url").textContent = g.url || "";
  $("mcp-awake").checked = !!g.keep_awake;
  for (const box of document.querySelectorAll(".link-only-box")) box.checked = !!g.link_only;
  $("mcp-auth-url").textContent = (g.oauth && g.oauth.authorize) || "";
  $("mcp-token-url").textContent = (g.oauth && g.oauth.token) || "";
  renderMcpActivity(g.activity || []);
  const d = $("mcp-approve");
  if (g.approval && state.approvalId !== g.approval.id) {
    state.approvalId = g.approval.id;
    const who = g.approval.client || "An AI app";
    $("ga-code").textContent = g.approval.code;
    $("ga-title").textContent = who;
    $("ga-client").textContent = who;
    if (!d.open) d.showModal();
  } else if (!g.approval && d.open) {
    d.close();
    state.approvalId = null;
  }
}

function renderMcpActivity(rows) {
  const list = $("mcp-activity-list");
  list.replaceChildren(...(rows.length ? rows.map((a) => el("li", {},
    el("span", { class: "act-time" }, ago(a.ts_ms)),
    el("span", { class: "act-q" }, a.query),
    el("span", { class: "act-n" }, a.error ? "error" : a.results == null ? "" : `${n(a.results)} result${a.results === 1 ? "" : "s"}`)))
    : [el("li", { class: "muted" }, "Nothing yet.")]));
}

function showMcpApp(app, remember = true) {
  if (!MCP_APPS.includes(app)) app = "grok";
  for (const t of document.querySelectorAll(".mcp-tabs [role=tab]")) {
    const sel = t.dataset.app === app;
    t.setAttribute("aria-selected", sel ? "true" : "false");
    t.tabIndex = sel ? 0 : -1;
  }
  for (const p of document.querySelectorAll(".mcp-panel")) p.hidden = p.dataset.app !== app;
  if (remember) { try { localStorage.setItem("mcp-app", app); } catch { /* not kept; fine */ } }
}

function savedMcpApp() {
  try { return localStorage.getItem("mcp-app") || "grok"; } catch { return "grok"; }
}

async function copyText(text, copied) {
  if (copied) return true;
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}

async function mcpToggle() {
  const want = $("mcp-toggle").checked;
  state.mcpBusy = true;
  if (want) {
    const r = await api().grok_enable();
    state.mcpBusy = false;
    if (!r.ok) { $("mcp-toggle").checked = false; toast(r.error); return; }
    const copied = await copyText(r.url, r.copied);
    toast(copied ? "Link copied. Paste it into your AI app." : "Copy the link below and paste it into your AI app.");
    renderMcp(r);
  } else {
    const r = await api().grok_disable();
    state.mcpBusy = false;
    if (r.ok) { renderMcp(r); toast("AI apps can't reach your data now."); }
  }
}

async function mcpCopy() {
  const r = await api().grok_copy_link();
  if (!r.ok) return toast(r.error);
  toast((await copyText(r.url, r.copied)) ? "Link copied." : "Select the link and copy it with ⌘C.");
}

async function mcpRegenerate() {
  const ok = await confirmDialog("Make a new link? Every connected AI app loses access right away. Then add the new link in each app (it's copied for you).", "Regenerate");
  if (!ok) return;
  const r = await api().grok_regenerate();
  if (!r.ok) return toast(r.error);
  const copied = await copyText(r.url, r.copied);
  toast(copied ? "New link copied. The old one no longer works." : "New link made. The old one no longer works.");
  renderMcp(r);
}

async function mcpSetLinkOnly(on) {
  const r = await api().grok_set_link_only(on);
  if (r.ok) renderMcp(r);
}

async function mcpAnswer(ok) {
  const id = state.approvalId;
  const who = $("ga-client").textContent || "The app";
  $("mcp-approve").close();
  const r = await api().grok_approve(id, ok);
  if (!r.ok) toast(r.error);
  else toast(ok ? `Approved. ${who} can now search your scrubbed data.` : "Denied.");
  refreshMcp();
}

// ---------- removed ----------
function bars(container, data) {
  container.replaceChildren();
  const entries = Object.entries(data);
  if (!entries.length) { container.append(el("div", { class: "muted" }, "Nothing yet.")); return; }
  const max = Math.max(...entries.map(([, v]) => v));
  for (const [label, v] of entries) {
    const fill = el("div", { class: "bar-fill" });
    fill.style.width = `${Math.max(3, (v / max) * 100)}%`;
    container.append(el("div", { class: "bar-row" }, el("span", {}, label), el("span", { class: "bar-n" }, n(v)),
      el("div", { class: "bar-track" }, fill)));
  }
}

async function loadRemoved() {
  const r = await api().privacy_report();
  if (!r.ok) return toast(r.error);
  $("t-kept").textContent = n(r.kept);
  $("t-dropped").textContent = n(r.dropped_total);
  $("t-redacted").textContent = n(Object.values(r.redacted).reduce((a, b) => a + b, 0));
  bars($("dropped-bars"), r.dropped);
  bars($("redacted-bars"), r.redacted);
  const by = {};
  for (const x of r.by_source || []) by[`${x.name} · kept`] = x.kept;
  for (const x of r.by_source || []) if (x.dropped) by[`${x.name} · skipped`] = x.dropped;
  bars($("by-source"), by);
}

// ---------- settings ----------
async function loadMcp() {
  const r = await api().mcp_config();
  if (r.ok) $("mcp-json").textContent = r.json;
}

async function disconnect(wipe) {
  const ok = await confirmDialog(
    wipe ? "Disconnect Gmail and permanently delete all scrubbed mail stored on this Mac? This can't be undone."
         : "Disconnect Gmail? Your scrubbed mail stays on this Mac, but it won't update until you reconnect.",
    wipe ? "Delete" : "Disconnect");
  if (!ok) return;
  const r = await api().disconnect(wipe);
  toast(r.ok ? (wipe ? "Disconnected and deleted." : "Disconnected.") : r.error);
  state.status = null;
  // Start Mail over: the next status refresh shows it again and reloads the list.
  Object.assign(mail, { rows: [], sel: -1, openKey: null, folders: null });
  $("mail-reader").replaceChildren(el("div", { class: "empty" }, "Select a conversation to read it."));
  state.shown = false;
  refreshStatus();
}

// ---------- connect ----------
async function connect(btn, errEl) {
  const mode = (document.querySelector('input[name="mode"]:checked') || {}).value
    || (state.status && state.status.mode) || "full";
  const email = $("ob-email").value.trim();
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Waiting for Google sign-in…";
  if (errEl) errEl.hidden = true;
  const r = await api().connect(mode, email);
  btn.disabled = false;
  btn.textContent = label;
  if (!r.ok) {
    if (errEl) { errEl.textContent = r.error; errEl.hidden = false; } else toast(r.error);
    return;
  }
  toast(`Connected ${r.account}`);
  refreshStatus();
}

// ---------- wiring ----------
function wire() {
  for (const b of document.querySelectorAll(".nav button")) b.addEventListener("click", () => show(b.dataset.view));
  for (const b of document.querySelectorAll("[data-goto]")) b.addEventListener("click", (e) => { e.preventDefault(); show(b.dataset.goto); });
  $("sync-now").addEventListener("click", async () => { await api().sync_now(); refreshStatus(); });
  $("ob-connect").addEventListener("click", () => connect($("ob-connect"), $("ob-error")));
  $("reconnect").addEventListener("click", () => connect($("reconnect"), null));
  $("s-disconnect").addEventListener("click", () => disconnect(false));
  $("s-wipe").addEventListener("click", () => disconnect(true));
  $("mcp-toggle").addEventListener("change", mcpToggle);
  $("mcp-link-copy").addEventListener("click", mcpCopy);
  for (const b of document.querySelectorAll("[data-open-app]")) b.addEventListener("click", () => api().open_connectors(b.dataset.openApp));
  for (const t of document.querySelectorAll(".mcp-tabs [role=tab]")) t.addEventListener("click", () => showMcpApp(t.dataset.app));
  $("mcp-regen").addEventListener("click", mcpRegenerate);
  $("mcp-awake").addEventListener("change", async (e) => {
    const r = await api().grok_set_keep_awake(e.target.checked);
    if (r.ok) toast(e.target.checked ? "Your Mac will stay awake while plugged in and MCP is on." : "Your Mac can sleep as usual.");
  });
  for (const box of document.querySelectorAll(".link-only-box")) box.addEventListener("change", (e) => mcpSetLinkOnly(e.target.checked));
  $("mcp-activity-btn").addEventListener("click", () => { $("mcp-activity").hidden = !$("mcp-activity").hidden; });
  $("mcp-help-btn").addEventListener("click", () => { $("mcp-help").hidden = !$("mcp-help").hidden; });
  $("mcp-clear").addEventListener("click", async () => { await api().grok_clear_activity(); refreshMcp(); });
  $("ga-ok").addEventListener("click", () => mcpAnswer(true));
  $("ga-deny").addEventListener("click", () => mcpAnswer(false));
  $("mcp-approve").addEventListener("cancel", (e) => e.preventDefault());
  showMcpApp(savedMcpApp(), false);
  $("mcp-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("mcp-json").textContent); toast("Copied."); }
    catch { toast("Select the text and copy it with ⌘C."); }
  });
}

function start() {
  wire();
  refreshStatus();
  refreshMcp();
  setInterval(refreshStatus, 1500);
  setInterval(refreshMcp, 1500);
}

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
