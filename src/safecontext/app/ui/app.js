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
let state = { status: null, view: "search", activeId: null, grok: null, grokBusy: false };

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
  const first = state.status === null;
  state.status = r;
  $("loading").hidden = true;
  const connected = !!r.account || r.any_connected;
  $("onboarding").hidden = connected;
  $("shell").hidden = !connected;
  if (!connected) return;

  $("demo-badge").hidden = !r.demo;
  const accounts = [...new Set((r.sources || []).filter((x) => x.account).map((x) => x.account))];
  $("account").textContent = r.account || accounts.join(", ");
  fillSourceFilter(r.sources || []);
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

  // Refresh the list while a sync fills the store, and once more when it finishes.
  const browsingRecent = state.view === "search" && !$("q").value;
  if (first) loadRecent();
  else if (browsingRecent && anySyncing) loadRecentThrottled();
  else if (browsingRecent && state.wasSyncing && !anySyncing) loadRecent();
  state.wasSyncing = anySyncing;
}

let lastRecent = 0;
function loadRecentThrottled() {
  if (Date.now() - lastRecent > 2500) loadRecent();
}

function show(view) {
  state.view = view;
  for (const b of document.querySelectorAll(".nav button")) b.classList.toggle("active", b.dataset.view === view);
  for (const v of ["search", "sources", "removed", "settings"]) $(`view-${v}`).hidden = v !== view;
  if (view === "sources") renderSources((state.status && state.status.sources) || []);
  if (view === "removed") loadRemoved();
  if (view === "settings") loadMcp();
}

// ---------- search ----------
function filters() {
  return [$("f-from").value.trim(), $("f-after").value, $("f-before").value, $("f-source").value];
}

function fillSourceFilter(sources) {
  const sel = $("f-source");
  const want = sources.filter((x) => x.available && (x.account || x.item_count)).map((x) => x.id);
  const have = [...sel.options].slice(1).map((o) => o.value);
  if (want.join() === have.join()) return;
  const keep = sel.value;
  sel.replaceChildren(el("option", { value: "" }, "All sources"),
    ...want.map((id) => el("option", { value: id }, sourceName(id))));
  sel.value = want.includes(keep) ? keep : "";
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
      state.status && state.status.account && !state.status.first_sync_done ? "Your mail is still being downloaded and scrubbed…" : "Nothing found."));
    return;
  }
  for (const h of rows) {
    const isEvent = h.kind === "event";
    const item = el("div", { class: "item" + (h.thread_id === state.activeId ? " active" : ""), role: "listitem", tabindex: "0" },
      el("div", { class: "item-top" },
        el("span", { class: "item-from" }, el("span", { class: "src-tag", title: sourceName(h.source) }, SOURCE_ICONS[h.source] || "•"),
          isEvent ? sourceName(h.source) : senderName(h.sender)),
        el("span", {}, isEvent ? fmtDate(h.date, true) : fmtDate(h.date))),
      el("div", { class: "item-subj" }, richText(h.title || h.subject || "(no subject)")),
      el("div", { class: "item-snip" }, richText((h.snippet || "").replace(/\s+/g, " "))));
    const open = () => {
      for (const x of list.querySelectorAll(".item.active")) x.classList.remove("active");
      item.classList.add("active");
      openThread(h.thread_id, h.source);
    };
    item.addEventListener("click", open);
    item.addEventListener("keydown", (e) => e.key === "Enter" && open());
    list.append(item);
  }
}

async function openThread(id, source = "") {
  state.activeId = id;
  const reader = $("reader");
  const r = await api().read_thread(id, source);
  reader.replaceChildren();
  if (!r.ok) { reader.append(el("div", { class: "empty" }, r.error)); return; }
  if (!r.messages.length) { reader.append(el("div", { class: "empty" }, r.note || "Nothing stored for this thread.")); return; }
  const first = r.messages[0];
  reader.append(el("h2", { class: "thread-subj" }, richText(first.title || first.subject || "(no subject)")));
  if (r.older_messages_omitted) reader.append(el("p", { class: "muted" }, `${r.older_messages_omitted} older items not shown.`));
  for (const m of r.messages) reader.append(m.kind === "event" ? eventCard(m) : messageCard(m));
}

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
  const [label, tone] = STATE_LABELS[x.state] || [x.state, ""];
  const soon = x.state === "coming_soon";
  const connected = !!x.account && !soon;
  const card = el("div", { class: "card src-card" + (soon ? " soon" : "") },
    el("div", { class: "src-head" },
      el("div", { class: "src-name" }, el("span", { class: "src-icon", "aria-hidden": "true" }, SOURCE_ICONS[x.id] || "•"), x.name),
      el("span", { class: `pill ${tone}` }, label)),
    el("p", { class: "src-desc" }, x.description),
    x.message ? el("div", { class: "src-msg" }, x.message) : null,
    el("div", { class: "needs-k" }, "Access it needs"),
    el("ul", { class: "needs" }, (x.requirements || []).map((q) =>
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

async function connectSource(x, btn) {
  state.sourcesBusy = true;
  btn.disabled = true;
  btn.textContent = x.requirements.some((q) => q.kind === "oauth_scope") ? "Waiting for Google sign-in…" : "Connecting…";
  const opts = x.id === "gmail" ? { mode: (state.status && state.status.mode) || "full" } : {};
  const r = await api().connect_source(x.id, opts);
  state.sourcesBusy = false;
  toast(r.ok ? `Connected ${x.name}` : r.error);
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

// ---------- use in grok ----------
const GROK_STATES = {
  off: ["Off", ""], connecting: ["Connecting…", "busy"], waiting: ["Waiting for Grok", "busy"],
  connected: ["Grok connected", "ok"], offline: ["Can't reach the relay. Retrying…", "warn"],
  rejected: ["Link not accepted. Regenerate it.", "warn"],
};

async function refreshGrok() {
  const r = await api().grok_status();
  if (r.ok) renderGrok(r);
}

function renderGrok(g) {
  state.grok = g;
  const on = g.enabled;
  if (!state.grokBusy) $("grok-toggle").checked = on;
  const [label, tone] = GROK_STATES[g.state] || [g.state, ""];
  let text = label;
  if (g.state === "connected" && g.last_used_ms) text += ` · last used ${ago(g.last_used_ms)}`;
  $("grok-status-text").textContent = text;
  $("grok-dot").className = "dot" + (tone === "ok" ? " on" : tone === "busy" ? " busy" : tone === "warn" ? " warn" : " off");
  $("grok-card").classList.toggle("is-on", on);
  $("grok-actions").hidden = !on;
  $("grok-on").hidden = !on;
  $("grok-pitch").hidden = on;
  // The three steps stay up until Grok has connected once.
  $("grok-steps").hidden = !on || g.state === "connected";
  $("grok-url").textContent = g.url || "";
  $("grok-awake").checked = !!g.keep_awake;
  $("grok-link-only").checked = !!g.link_only;
  $("grok-auth-url").textContent = (g.oauth && g.oauth.authorize) || "";
  $("grok-token-url").textContent = (g.oauth && g.oauth.token) || "";
  renderGrokActivity(g.activity || []);
  const d = $("grok-approve");
  if (g.approval && state.approvalId !== g.approval.id) {
    state.approvalId = g.approval.id;
    $("ga-code").textContent = g.approval.code;
    $("ga-client").textContent = g.approval.client || "Grok";
    if (!d.open) d.showModal();
  } else if (!g.approval && d.open) {
    d.close();
    state.approvalId = null;
  }
}

function renderGrokActivity(rows) {
  const list = $("grok-activity-list");
  list.replaceChildren(...(rows.length ? rows.map((a) => el("li", {},
    el("span", { class: "act-time" }, ago(a.ts_ms)),
    el("span", { class: "act-q" }, a.query),
    el("span", { class: "act-n" }, a.error ? "error" : a.results == null ? "" : `${n(a.results)} result${a.results === 1 ? "" : "s"}`)))
    : [el("li", { class: "muted" }, "Nothing yet.")]));
}

async function copyText(text, copied) {
  if (copied) return true;
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}

async function grokToggle() {
  const want = $("grok-toggle").checked;
  state.grokBusy = true;
  if (want) {
    const r = await api().grok_enable();
    state.grokBusy = false;
    if (!r.ok) { $("grok-toggle").checked = false; toast(r.error); return; }
    const copied = await copyText(r.url, r.copied);
    toast(copied ? "Link copied. Paste it into Grok." : "Copy the link below and paste it into Grok.");
    api().open_grok_connectors();
    renderGrok(r);
  } else {
    const r = await api().grok_disable();
    state.grokBusy = false;
    if (r.ok) { renderGrok(r); toast("Grok can't reach your data now."); }
  }
}

async function grokCopy() {
  const r = await api().grok_copy_link();
  if (!r.ok) return toast(r.error);
  toast((await copyText(r.url, r.copied)) ? "Link copied." : "Select the link and copy it with ⌘C.");
}

async function grokRegenerate() {
  const ok = await confirmDialog("Make a new link? Grok loses access right away. Then add the new link in Grok (it's copied for you).", "Regenerate");
  if (!ok) return;
  const r = await api().grok_regenerate();
  if (!r.ok) return toast(r.error);
  const copied = await copyText(r.url, r.copied);
  toast(copied ? "New link copied. The old one no longer works." : "New link made. The old one no longer works.");
  renderGrok(r);
}

async function grokAnswer(ok) {
  const id = state.approvalId;
  $("grok-approve").close();
  const r = await api().grok_approve(id, ok);
  if (!r.ok) toast(r.error);
  else toast(ok ? "Approved. Grok can now search your scrubbed data." : "Denied.");
  refreshGrok();
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
  $("results").replaceChildren();
  $("reader").replaceChildren(el("div", { class: "empty" }, "Select an item to read it."));
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
  $("f-source").addEventListener("change", () => doSearch());
  $("search-form").addEventListener("submit", doSearch);
  $("q").addEventListener("search", () => !$("q").value && loadRecent());
  $("sync-now").addEventListener("click", async () => { await api().sync_now(); refreshStatus(); });
  $("ob-connect").addEventListener("click", () => connect($("ob-connect"), $("ob-error")));
  $("reconnect").addEventListener("click", () => connect($("reconnect"), null));
  $("s-disconnect").addEventListener("click", () => disconnect(false));
  $("s-wipe").addEventListener("click", () => disconnect(true));
  $("grok-toggle").addEventListener("change", grokToggle);
  $("grok-copy").addEventListener("click", grokCopy);
  $("grok-open").addEventListener("click", () => api().open_grok_connectors());
  $("grok-regen").addEventListener("click", grokRegenerate);
  $("grok-awake").addEventListener("change", async (e) => {
    const r = await api().grok_set_keep_awake(e.target.checked);
    if (r.ok) toast(e.target.checked ? "Your Mac will stay awake while plugged in and Grok is on." : "Your Mac can sleep as usual.");
  });
  $("grok-link-only").addEventListener("change", (e) => api().grok_set_link_only(e.target.checked));
  $("grok-activity-btn").addEventListener("click", () => { $("grok-activity").hidden = !$("grok-activity").hidden; });
  $("grok-help-btn").addEventListener("click", () => { $("grok-help").hidden = !$("grok-help").hidden; });
  $("grok-clear").addEventListener("click", async () => { await api().grok_clear_activity(); refreshGrok(); });
  $("ga-ok").addEventListener("click", () => grokAnswer(true));
  $("ga-deny").addEventListener("click", () => grokAnswer(false));
  $("grok-approve").addEventListener("cancel", (e) => e.preventDefault());
  $("mcp-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("mcp-json").textContent); toast("Copied."); }
    catch { toast("Select the text and copy it with ⌘C."); }
  });
}

function start() {
  wire();
  refreshStatus();
  refreshGrok();
  setInterval(refreshStatus, 1500);
  setInterval(refreshGrok, 1500);
}

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
