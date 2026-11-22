// MyContext window. All mail content is inserted with textContent / text nodes,
// never parsed as HTML, so nothing in an email can run code or load resources.
"use strict";

const REDACTION_NAMES = {
  ONE_TIME_CODE: "2FA code", TOTP_SECRET: "authenticator key", BACKUP_CODE: "backup code",
  CREDIT_CARD: "card number", CVV: "card code", SSN: "SSN", API_KEY: "API key", JWT: "login token",
  PRIVATE_KEY: "private key", PASSWORD: "password", IBAN: "IBAN", ROUTING_NUMBER: "routing no.",
  BANK_ACCOUNT: "account no.", DATE_OF_BIRTH: "birth date", PHONE: "phone", STREET_ADDRESS: "address",
  HEALTH: "health detail", LEGAL: "legal detail", PERSONAL: "personal detail",
};

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
let state = { status: null, view: "search", activeId: null, asking: false };

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
  const re = /\[REDACTED:([A-Z_]+)\]|«([^»]*)»|(https:\/\/mail\.google\.com\/mail\/[^\s)\]]+)/g;
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
      frag.append(el("a", { onclick: () => openGmail(url) }, "Open in Gmail"));
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

async function openGmail(url) {
  const r = await api().open_in_gmail(url);
  if (!r.ok) toast(r.error);
}

// ---------- status / shell ----------
async function refreshStatus() {
  const r = await api().status();
  if (!r.ok) return;
  const first = state.status === null;
  state.status = r;
  $("loading").hidden = true;
  const connected = !!r.account;
  $("onboarding").hidden = connected;
  $("shell").hidden = !connected;
  if (!connected) return;

  $("demo-badge").hidden = !r.demo;
  $("account").textContent = r.account;
  $("banner").hidden = !r.needs_reconnect;
  $("banner-text").textContent = r.reconnect_message;

  const dot = $("sync-dot");
  dot.className = "dot" + (r.syncing ? " busy" : r.needs_reconnect || (r.last_result && r.last_result.status !== "ok") ? " warn" : "");
  if (r.syncing) {
    const p = r.progress;
    $("sync-text").textContent = r.first_sync_done ? "Checking for new mail…" : "Downloading & scrubbing…";
    $("sync-sub").textContent = `${n(p.kept)} kept · ${n(p.dropped)} skipped`;
  } else if (r.needs_reconnect) {
    $("sync-text").textContent = "Paused";
    $("sync-sub").textContent = "Gmail needs reconnecting";
  } else if (r.last_result && r.last_result.status !== "ok") {
    $("sync-text").textContent = "Will retry";
    $("sync-sub").textContent = r.last_result.message;
  } else {
    $("sync-text").textContent = "Up to date";
    $("sync-sub").textContent = `${n(r.kept)} messages · ${r.last_sync_ms ? ago(r.last_sync_ms) : "never synced"}`;
  }
  $("sync-now").disabled = r.syncing || r.needs_reconnect;
  $("no-key").hidden = r.has_xai_key;

  $("s-account").textContent = r.account;
  $("s-mode").textContent = r.mode === "headers" ? "Headers only (no message text)" : "Full messages, scrubbed";
  $("s-dir").textContent = r.data_dir;
  $("s-key").textContent = r.demo ? "Not needed in demo mode" : r.has_xai_key ? "Saved in Keychain" : "Not set";
  if (document.activeElement !== $("model-input")) $("model-input").value = r.model;

  // Refresh the list while a sync fills the store, and once more when it finishes.
  const browsingRecent = state.view === "search" && !$("q").value;
  if (first) loadRecent();
  else if (browsingRecent && r.syncing) loadRecentThrottled();
  else if (browsingRecent && state.wasSyncing && !r.syncing) loadRecent();
  state.wasSyncing = r.syncing;
}

let lastRecent = 0;
function loadRecentThrottled() {
  if (Date.now() - lastRecent > 2500) loadRecent();
}

function show(view) {
  state.view = view;
  for (const b of document.querySelectorAll(".nav button")) b.classList.toggle("active", b.dataset.view === view);
  for (const v of ["search", "ask", "removed", "settings"]) $(`view-${v}`).hidden = v !== view;
  if (view === "removed") loadRemoved();
  if (view === "settings") loadMcp();
  if (view === "ask") $("ask-input").focus();
}

// ---------- search ----------
function filters() {
  return [$("f-from").value.trim(), $("f-after").value, $("f-before").value];
}

async function loadRecent() {
  lastRecent = Date.now();
  const r = await api().recent(...filters());
  renderResults(r, "Most recent");
}

async function doSearch(e) {
  e && e.preventDefault();
  const q = $("q").value.trim();
  if (!q) return loadRecent();
  const r = await api().search(q, ...filters());
  renderResults(r, `Results for “${q}”`);
}

function renderResults(r, heading) {
  const list = $("results");
  list.replaceChildren();
  if (!r.ok) { list.append(el("div", { class: "empty" }, r.error)); return; }
  const rows = r.results || [];
  list.append(el("div", { class: "list-head" }, `${heading} · ${rows.length}`, r.notice ? ` · ${r.notice}` : ""));
  if (!rows.length) {
    list.append(el("div", { class: "empty" },
      state.status && !state.status.first_sync_done ? "Your mail is still being downloaded and scrubbed…" : "Nothing found."));
    return;
  }
  for (const h of rows) {
    const item = el("div", { class: "item" + (h.thread_id === state.activeId ? " active" : ""), role: "listitem", tabindex: "0" },
      el("div", { class: "item-top" }, el("span", { class: "item-from" }, senderName(h.sender)), el("span", {}, fmtDate(h.date))),
      el("div", { class: "item-subj" }, richText(h.subject || "(no subject)")),
      el("div", { class: "item-snip" }, richText((h.snippet || "").replace(/\s+/g, " "))));
    const open = () => {
      for (const x of list.querySelectorAll(".item.active")) x.classList.remove("active");
      item.classList.add("active");
      openThread(h.thread_id);
    };
    item.addEventListener("click", open);
    item.addEventListener("keydown", (e) => e.key === "Enter" && open());
    list.append(item);
  }
}

async function openThread(id) {
  state.activeId = id;
  const reader = $("reader");
  const r = await api().read_thread(id);
  reader.replaceChildren();
  if (!r.ok) { reader.append(el("div", { class: "empty" }, r.error)); return; }
  if (!r.messages.length) { reader.append(el("div", { class: "empty" }, r.note || "Nothing stored for this thread.")); return; }
  reader.append(el("h2", { class: "thread-subj" }, richText(r.messages[0].subject || "(no subject)")));
  if (r.older_messages_omitted) reader.append(el("p", { class: "muted" }, `${r.older_messages_omitted} older messages not shown.`));
  for (const m of r.messages) {
    const atts = (m.attachments || []).map((a) =>
      el("span", { class: "att", title: "Attachment contents are never downloaded" },
        "📎 ", richText(a.filename), " · ", a.mime_type, a.size ? ` · ${fmtSize(a.size)}` : ""));
    reader.append(el("article", { class: "msg" },
      el("div", { class: "msg-head" },
        el("div", {},
          el("div", { class: "msg-from" }, m.sender),
          el("div", { class: "msg-meta" }, fmtDate(m.date, true), m.recipients ? ` · to ${m.recipients}` : "")),
        el("button", { class: "btn small", onclick: () => openGmail(m.gmail_link) }, "Open in Gmail")),
      el("div", { class: "msg-body" }, m.body ? richText(m.body) : el("span", { class: "muted" },
        state.status && state.status.mode === "headers" ? "Headers-only mode: no message text is stored." : "(empty)")),
      m.body_truncated ? el("p", { class: "muted" }, "Long message shortened. Open in Gmail for the rest.") : null,
      atts.length ? el("div", { class: "atts" }, atts) : null));
  }
}

// ---------- ask grok ----------
function addBubble(kind, content) {
  $("suggestions").hidden = true;
  const b = el("div", { class: `bubble ${kind}` }, content);
  $("transcript").append(b);
  b.scrollIntoView({ block: "end", behavior: "smooth" });
  return b;
}

async function ask(e) {
  e && e.preventDefault();
  const q = $("ask-input").value.trim();
  if (!q || state.asking) return;
  state.asking = true;
  $("ask-send").disabled = true;
  $("ask-input").value = "";
  addBubble("user", q);
  const pending = addBubble("bot", el("span", { class: "thinking" }, "Searching your scrubbed mail…"));
  const r = await api().ask(q);
  pending.replaceChildren();
  if (!r.ok) {
    pending.classList.add("err");
    pending.append(r.error);
  } else {
    // Strip the auto-added plain-text footer; sources are shown as cards instead.
    const body = r.text.replace(/\n\nSources:\n[\s\S]*$/, "");
    pending.append(richText(body, { links: true }));
    if (r.sources && r.sources.length) {
      pending.append(el("div", { class: "sources" },
        el("div", { class: "muted" }, "Sources"),
        r.sources.slice(0, 8).map((s) => el("div", { class: "source" },
          el("span", {}, el("b", {}, richText(s.subject || "(no subject)")), ` · ${senderName(s.sender)} · ${fmtDate(s.date)}`),
          el("button", { class: "btn small", onclick: () => openGmail(s.gmail_link) }, "Open in Gmail")))));
    }
  }
  pending.scrollIntoView({ block: "end", behavior: "smooth" });
  state.asking = false;
  $("ask-send").disabled = false;
  $("ask-input").focus();
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
  $("results").replaceChildren();
  $("reader").replaceChildren(el("div", { class: "empty" }, "Select a message to read it."));
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
  for (const b of document.querySelectorAll("[data-goto]")) b.addEventListener("click", () => show(b.dataset.goto));
  $("search-form").addEventListener("submit", doSearch);
  $("q").addEventListener("search", () => !$("q").value && loadRecent());
  $("ask-form").addEventListener("submit", ask);
  $("ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) ask(e); });
  for (const s of document.querySelectorAll("#suggestions .chip-btn"))
    s.addEventListener("click", () => { $("ask-input").value = s.textContent; ask(); });
  $("new-chat").addEventListener("click", async () => {
    await api().new_chat();
    $("transcript").replaceChildren($("suggestions"));
    $("suggestions").hidden = false;
  });
  $("sync-now").addEventListener("click", async () => { await api().sync_now(); refreshStatus(); });
  $("ob-connect").addEventListener("click", () => connect($("ob-connect"), $("ob-error")));
  $("reconnect").addEventListener("click", () => connect($("reconnect"), null));
  $("s-disconnect").addEventListener("click", () => disconnect(false));
  $("s-wipe").addEventListener("click", () => disconnect(true));
  $("key-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const r = await api().set_xai_key($("key-input").value);
    $("key-error").hidden = r.ok;
    $("key-error").textContent = r.ok ? "" : r.error;
    if (r.ok) { $("key-input").value = ""; toast("Key saved to Keychain."); refreshStatus(); }
  });
  $("key-clear").addEventListener("click", async () => { await api().clear_xai_key(); toast("Key removed."); refreshStatus(); });
  $("model-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const r = await api().set_model($("model-input").value);
    toast(r.ok ? `Using ${r.model}` : r.error);
  });
  $("mcp-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("mcp-json").textContent); toast("Copied."); }
    catch { toast("Select the text and copy it with ⌘C."); }
  });
}

function start() {
  wire();
  refreshStatus();
  setInterval(refreshStatus, 1500);
}

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
