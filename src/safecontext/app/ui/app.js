// SafeContext window. All mail, event and chat content is inserted with textContent /
// text nodes, never parsed as HTML, so nothing in an item can run code or load resources.
"use strict";

const REDACTION_NAMES = {
  ONE_TIME_CODE: "2FA code", TOTP_SECRET: "authenticator key", BACKUP_CODE: "backup code",
  CREDIT_CARD: "card number", CVV: "card code", SSN: "SSN", API_KEY: "API key", JWT: "login token",
  PRIVATE_KEY: "private key", PASSWORD: "password", IBAN: "IBAN", ROUTING_NUMBER: "routing no.",
  BANK_ACCOUNT: "account no.", DATE_OF_BIRTH: "birth date", PHONE: "phone", STREET_ADDRESS: "address",
  BOOKING_CODE: "booking code",
  HEALTH: "health detail", LEGAL: "legal detail", PERSONAL: "personal detail",
};

const REQ_KINDS = { oauth_scope: "Google permission", macos_permission: "Mac permission", file: "File", folder: "Folder" };
const SOURCE_ICONS = { gmail: "✉️", gcal: "📅", imessage: "💬", whatsapp: "🟢", recordings: "🎙️" };
const OPEN_LABELS = { gmail: "Open in Gmail", gcal: "Open in Calendar" };
const STATE_LABELS = {
  connected: ["Connected", "ok"], syncing: ["Syncing…", "busy"], needs_reconnect: ["Needs reconnecting", "warn"],
  needs_permission: ["Needs permission", "warn"], error: ["Will retry", "warn"], not_connected: ["Not connected", ""],
  coming_soon: ["Coming soon", ""],
};

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
let state = { status: null, view: "removed", shown: false,  // opens on the Security Dashboard
              mcp: null, mcpBusy: false };

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
  if (allDay) {
    // All-day dates are stored as UTC midnight with an exclusive end: read them in UTC,
    // or anyone west of Greenwich sees the day before.
    const day = (ms) => { const d = new Date(ms), o = { month: "short", day: "numeric", timeZone: "UTC" };
      if (d.getUTCFullYear() !== new Date().getFullYear()) o.year = "numeric";
      return d.toLocaleDateString(undefined, o); };
    const last = end ? new Date(end).getTime() - 86400000 : 0;
    return day(start) + (last > new Date(start).getTime() ? ` – ${day(last)}` : "") + " · all day";
  }
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

  const accounts = [...new Set((r.sources || []).filter((x) => x.account).map((x) => x.account))];
  const who = r.account || accounts.join(", ");
  $("account").textContent = who;
  $("acct-menu-email").textContent = who;
  $("acct-btn").title = who;
  $("acct-avatar").textContent = (who.trim()[0] || "?").toUpperCase();
  if (state.view === "sources") renderSources(r.sources || []);
  if (state.view === "source") renderSourcePage();
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
    const lastOther = Math.max(0, ...others.map((x) => x.last_sync_ms || 0));
    $("sync-sub").textContent = `${n(r.total_items)} items · ${others.map((x) => x.name).join(", ")}` +
      (busy || !lastOther ? "" : ` · checked ${ago(lastOther)}`);
  } else if (r.needs_reconnect) {
    $("sync-text").textContent = "Paused";
    $("sync-sub").textContent = "Gmail needs reconnecting";
  } else if (r.last_result && r.last_result.status !== "ok") {
    $("sync-text").textContent = "Will retry";
    $("sync-sub").textContent = r.last_result.message;
  } else if (!r.first_sync_done) {
    $("sync-text").textContent = "Paused";
    $("sync-sub").textContent = `${n(r.kept)} messages so far${r.oldest_ms ? " · back to " + monthYear(r.oldest_ms) : ""}. Resumes automatically.`;
  } else {
    $("sync-text").textContent = "Up to date";
    const extra = r.total_items > r.kept ? ` · ${n(r.total_items - r.kept)} other items` : "";
    $("sync-sub").textContent = `${n(r.kept)} messages${extra} · ${r.last_sync_ms ? "checked " + ago(r.last_sync_ms) : "never synced"}`;
  }
  const anySyncing = (r.sources || []).some((x) => x.syncing);
  // The card shows while a sync runs or something needs attention (a reconnect, a failed sync, an unfinished
  // first sync), and steps aside once everything is up to date. Sync status (account menu) always has the details.
  $("sync").hidden = !(r.syncing || anySyncing || dot.classList.contains("warn") || (!!r.account && !r.first_sync_done));
  $("sync-now").disabled = anySyncing || (!!r.account && r.needs_reconnect && !(r.sources || []).some((x) => x.id !== "gmail" && x.account));
  $("s-check-now").disabled = $("sync-now").disabled;

  $("s-account").textContent = r.account || "Not connected";
  $("s-mode").textContent = r.mode === "headers" ? "Headers only (no message text)" : "Full messages, scrubbed";
  $("s-dir").textContent = r.data_dir;

  renderNavSources();
  if (!state.shown) { state.shown = true; show(state.view); }  // first time the window has a screen to show
}

function show(view) {
  if (view === "mail" && !pickSource()) view = "sources";  // nothing to browse yet
  state.view = view;
  markNav();
  for (const v of ["mail", "sources", "source", "mcp", "removed", "review", "sync", "settings", "files"]) $(`view-${v}`).hidden = v !== view;
  if (view === "mail") mailShow();
  if (view === "sources") renderSources((state.status && state.status.sources) || []);
  if (view === "source") renderSourcePage();
  if (view === "removed") loadRemoved();
  if (view === "sync") syncPageShow();
  if (view === "review") loadReview();
  if (view === "mcp") loadMcp();
  if (view === "files") loadFiles();
  navRecord();
}

// ---------- files: folders and PDFs the user opts in ----------
function fileRow(entry) {
  const kind = entry.kind === "folder" ? "Folder" : "PDF";
  return el("div", { class: "file-row" },
    el("div", {}, el("span", { class: "file-kind" }, kind), el("code", {}, entry.path)),
    el("button", { class: "btn small", onclick: () => removeFile(entry.path) }, "Remove"));
}

async function loadFiles(keepError) {
  const r = await api().pdf_selection();
  const err = $("files-error");
  if (!r.ok) {
    err.hidden = false;
    err.textContent = r.error;
    return;
  }
  if (!keepError) err.hidden = true;
  const entries = r.entries || [];
  $("files-entries").replaceChildren(entries.length
    ? el("div", { class: "file-list" }, entries.map(fileRow))
    : el("p", { class: "muted" }, "Nothing added yet. No PDF text can be read."));
  const pdfs = r.pdfs || [];
  $("files-pdfs").replaceChildren(pdfs.length
    ? el("div", { class: "file-list" }, pdfs.map((p) => el("div", { class: "file-row" }, el("code", {}, p))))
    : el("p", { class: "muted" }, "No PDFs in the folders and files you added."));
}

async function addFiles(kind) {
  const r = await api().pdf_pick(kind);
  const err = $("files-error");
  if (!r.ok) {
    err.hidden = false;
    err.textContent = r.error;
    loadFiles(true);  // a later file can fail after an earlier one was saved
    return;
  }
  err.hidden = true;
  if (!r.cancelled) loadFiles();
}

async function removeFile(path) {
  const r = await api().pdf_remove(path);
  const err = $("files-error");
  if (!r.ok) {
    err.hidden = false;
    err.textContent = r.error;
    return;
  }
  err.hidden = true;
  loadFiles();
}

// ---------- header: hide the sidebar, back / forward ----------
const SIDEBAR_KEY = "safecontext.sidebarHidden";

// Below this width the sidebar collapses and opens as a floating panel over the page (app.css, same number).
const SIDEBAR_COLLAPSE = window.matchMedia("(max-width: 1000px)");

function setSidebarHidden(hidden, save = true) {
  $("shell").classList.toggle("sidebar-hidden", hidden);
  if (save) { try { localStorage.setItem(SIDEBAR_KEY, hidden ? "1" : ""); } catch (e) { /* this session only */ } }
  sidebarButton();
}

// Narrow windows: the sidebar floats over the page while open.
function setSidebarOpen(open) {
  $("shell").classList.toggle("sidebar-open", open);
  sidebarButton();
}

// The header button: on wide windows it hides or shows the sidebar (remembered); on narrow ones it opens
// or closes the floating panel.
function toggleSidebar() {
  const shell = $("shell");
  if (SIDEBAR_COLLAPSE.matches) setSidebarOpen(!shell.classList.contains("sidebar-open"));
  else setSidebarHidden(!shell.classList.contains("sidebar-hidden"));
}

function sidebarButton() {
  const shell = $("shell");
  const showing = SIDEBAR_COLLAPSE.matches ? shell.classList.contains("sidebar-open") : !shell.classList.contains("sidebar-hidden");
  const b = $("nav-sidebar"), label = showing ? "Hide sidebar" : "Show sidebar";
  b.setAttribute("aria-label", label);
  b.setAttribute("aria-pressed", String(!showing));
  b.title = `${label} (⌘\\)`;
}

// Pages visited, for Back and Forward. A place is a page plus which source page or mailbox it showed.
const pageHistory = { back: [], fwd: [], cur: null, restoring: false };

function navPlace() {
  const mailSource = typeof mail !== "undefined" ? mail.source : "";  // mail.js may not have loaded yet
  return { view: state.view, sourceId: state.sourceId || "", mailSource: mailSource || "" };
}

function samePlace(a, b) {
  return a.view === b.view && (a.view !== "source" || a.sourceId === b.sourceId)
    && (a.view !== "mail" || a.mailSource === b.mailSource);
}

// Called by show(): a new place goes on the Back list and clears Forward (like a browser).
function navRecord() {
  const now = navPlace();
  if (!pageHistory.restoring && pageHistory.cur && !samePlace(pageHistory.cur, now)) {
    pageHistory.back.push(pageHistory.cur);
    pageHistory.fwd = [];
  }
  pageHistory.cur = now;
  $("nav-back").disabled = !pageHistory.back.length;
  $("nav-fwd").disabled = !pageHistory.fwd.length;
}

function navGo(step) {  // -1 back, +1 forward
  const from = step < 0 ? pageHistory.back : pageHistory.fwd;
  const to = step < 0 ? pageHistory.fwd : pageHistory.back;
  if (!from.length) return;
  to.push(pageHistory.cur);
  const place = from.pop();
  pageHistory.restoring = true;
  try {
    state.sourceId = place.sourceId;
    if (place.view === "mail" && place.mailSource && place.mailSource !== mail.source) openSource(place.mailSource);
    else show(place.view);
  } finally {
    pageHistory.restoring = false;
  }
}

// ---------- sources in the sidebar ----------
// Sources (the connect/manage page) is the parent; each source with something to show sits under it
// and opens the browse page (mail.js) for just that source.
function navSources() {
  return ((state.status && state.status.sources) || []).filter((x) => x.available && (x.account || x.item_count));
}

function pickSource() {
  const ids = navSources().map((x) => x.id);
  if (!ids.includes(mail.source)) mail.source = ids[0] || "";
  return mail.source;
}

function renderNavSources() {
  const list = navSources();
  const box = $("nav-sources");
  const sig = list.map((x) => `${x.id}:${x.name}`).join();
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.replaceChildren(...list.map((x) => el("button", { class: "nav-child", "data-source": x.id, onclick: () => openSource(x.id) },
      sourceIcon(x.id, "nav-ico"), x.name)));
  }
  markNav();
}

function markNav() {
  for (const b of document.querySelectorAll(".nav [data-view]"))  // a source's page counts as Sources
    b.classList.toggle("active", b.dataset.view === state.view || (b.dataset.view === "sources" && state.view === "source"));
  for (const b of document.querySelectorAll("[data-menu-view]")) b.classList.toggle("active", b.dataset.menuView === state.view);
  $("acct-btn").classList.toggle("active", state.view === "settings" || state.view === "sync");
  for (const b of document.querySelectorAll(".nav [data-source]")) {
    const on = state.view === "mail" && b.dataset.source === mail.source;
    b.classList.toggle("active", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
}

function openSource(id) {
  mailInit();  // the mail list may not have been shown yet (the app opens on the Security Dashboard)
  if (mail.source !== id) {
    Object.assign(mail, { source: id, rows: [], next: null, sel: -1, openKey: null, query: "", reasons: [] });
    $("mail-q").value = "";
    $("mail-reader").replaceChildren(el("div", { class: "empty" }, "Select an item to read it."));
    mailReaderBar();
    mailSetView("all");
  }
  show("mail");
}

// ---------- reader cards (used by mail.js) ----------
function messageCard(m) {
  const atts = (m.attachments || []).map((a) =>
    el("span", { class: "att", title: "Attachment contents are never downloaded" },
      "📎 ", richText(a.filename), " · ", a.mime_type, a.size ? ` · ${fmtSize(a.size)}` : ""));
  const to = m.recipients || m.participants;
  return el("article", { class: "msg" },
    el("div", { class: "msg-head" },
      el("div", {},
        el("div", { class: "msg-from" }, m.sender),
        el("div", { class: "msg-meta" }, fmtDate(m.date, true), to ? ` · to ${to}` : ""))),  // "Open in Gmail" is in the reader's bar
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
      el("div", { class: "msg-meta" }, "📅 ", sourceName(m.source))),
    el("dl", { class: "details" }, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, richText(String(v)))])),
    m.body ? el("div", { class: "msg-body" }, richText(m.body)) : null,
    m.body_truncated ? el("p", { class: "muted" }, "Long description shortened. Open the original for the rest.") : null);
}

// ---------- sources ----------
// A Lucide-style line icon (24×24 viewBox, stroked in the text color) from its path data.
function icon(paths) {
  const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const d of paths) { const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); svg.append(p); }
  return svg;
}

// ---------- sources: a grid of tiles, and a page per source ----------
// A source's icon: an SVG from a <template id="icon-<id>"> in index.html when there is one, else its emoji.
function sourceIcon(id, cls = "src-icon") {
  const t = document.getElementById(`icon-${id}`);
  return el("span", { class: cls, "aria-hidden": "true" }, t ? t.content.cloneNode(true) : (SOURCE_ICONS[id] || "•"));
}

// [label, tone, missing permission or null] for a source's status pill.
function sourceStatus(x) {
  let [label, tone] = STATE_LABELS[x.state] || [x.state, ""];
  const perm = x.state === "needs_permission" ? x.permission : null;
  if (perm) label = `Needs ${perm.title}`;
  return [label, tone, perm];
}

function renderSources(list) {
  if (state.sourcesBusy) return;  // don't redraw under a pending click
  $("source-grid").replaceChildren(...list.map(sourceTile));
}

// The Sources grid shows only icon, name, description and status; everything else is on the source's page.
function sourceTile(x) {
  return el("button", { class: "src-tile" + (x.state === "coming_soon" ? " soon" : ""), type: "button",
    onclick: () => openSourcePage(x.id) },
  sourceIcon(x.id, "src-icon lg"),
  el("span", { class: "src-tile-main" }, el("span", { class: "src-tile-name" }, x.name),
    el("span", { class: "src-tile-desc" }, x.description)),
  sourceTileStatus(x));
}

// A connected source's ⋯ menu on its page: Disconnect lives here, not as a button, so it's harder to hit by accident.
function sourceMoreMenu(x) {
  const menu = el("sc-menu", { icon: "ellipsis", label: `More options for ${x.name}`, align: "end", compact: "" },
    el("button", { class: "sc-item danger", role: "menuitem", onclick: () => { menu.hide(); disconnectSource(x); } }, "Disconnect"));
  return menu;
}

// The tile's status: a green check when connected, a plus when not, a text pill for anything else
// (coming soon, needs permission or reconnecting, will retry). The check and plus are the app's standard
// icon button (.icon-btn; the plus is the secondary variant) with tooltips; like the rest of the tile they open the source's page (the tile
// itself is the button, so they're spans, not nested buttons).
function sourceTileStatus(x) {
  if (x.state === "connected" || x.state === "syncing") {
    const tip = x.state === "syncing" ? `Connected · Syncing ${x.name}…` : `Connected · Open ${x.name}`;
    return el("span", { class: "icon-btn src-state-btn ok", title: tip, "aria-label": tip }, icon(["M20 6 9 17l-5-5"]));
  }
  if (x.state === "not_connected") {
    const tip = `Not connected · Open to connect ${x.name}`;
    return el("span", { class: "icon-btn secondary src-state-btn", title: tip, "aria-label": tip }, icon(["M5 12h14", "M12 5v14"]));
  }
  const [label, tone] = sourceStatus(x);
  return el("span", { class: `pill ${tone}` }, label);
}

function openSourcePage(id) {
  state.sourceId = id;
  show("source");
}

// One source's page: header with its actions, then what's on this Mac and the access it needs,
// with the facts (status, account, access, last sync) in a side column.
function renderSourcePage() {
  if (state.sourcesBusy) return;
  const x = ((state.status && state.status.sources) || []).find((s) => s.id === state.sourceId);
  const page = $("view-source");
  if (!x) { page.replaceChildren(); return; }
  const [label, tone, perm] = sourceStatus(x);
  const soon = x.state === "coming_soon";
  const connected = !!x.account && !soon;

  const actions = el("div", { class: "sp-actions" });
  if (soon) {
    actions.append(el("button", { class: "btn lg-ish", disabled: "" }, "Coming soon"));
  } else if (perm) {
    // The permission box carries Open System Settings; here: try again, or back out.
    actions.append(el("button", { class: "btn", onclick: (e) => connectSource(x, e.target) }, "Check again"));
    if (connected) actions.append(sourceMoreMenu(x));
    else actions.append(el("button", { class: "btn", onclick: () => cancelSource(x) }, "Not now"));
  } else if (!connected) {
    actions.append(el("button", { class: "btn primary lg-ish", onclick: (e) => connectSource(x, e.target) }, "Connect"));
  } else {
    if (x.state === "needs_reconnect")
      actions.append(el("button", { class: "btn primary", onclick: (e) => connectSource(x, e.target) }, "Reconnect"));
    // No per-source sync button: every source checks itself (launch, focus, wake, every few minutes),
    // and the sidebar's Check now covers "right now".
    actions.append(sourceMoreMenu(x));
    // Browse sits right of the ⋯ menu, for sources that have a browse page (Gmail today).
    if (navSources().some((s) => s.id === x.id))
      actions.append(el("button", { class: "btn primary", onclick: () => openSource(x.id) }, `Browse ${x.name}`));
  }

  const main = el("div", { class: "sp-main" });
  if (x.message && !perm) main.append(el("div", { class: "src-msg" }, x.message));
  if (perm) main.append(permissionBox(x, perm));
  if (connected) {
    const tile = (num, lbl) => el("div", { class: "sp-tile" }, el("div", { class: "sp-num" }, num), el("div", { class: "sp-lbl" }, lbl));
    main.append(el("h3", {}, "On this Mac"),
      el("div", { class: "sp-tiles" }, tile(n(x.item_count), "kept, scrubbed"), tile(n(x.dropped_count), "skipped"),
        tile(x.syncing ? "now" : x.last_sync_ms ? ago(x.last_sync_ms) : "—", "last sync")));
  }
  // What access the source needs: each one's name is its own heading (no card, no "Access it needs" over it).
  for (const q of x.requirements || [])
    main.append(el("section", { class: "sp-need" },
      el("h3", {}, q.label, REQ_KINDS[q.kind] ? el("span", { class: "pill" }, REQ_KINDS[q.kind]) : null),
      el("p", {}, q.why)));

  const meta = (k, v) => v ? el("div", { class: "sp-meta-row" }, el("div", { class: "sp-k" }, k), el("div", {}, v)) : null;
  const aside = el("aside", { class: "sp-meta" },
    meta("Status", el("span", { class: `pill ${tone}` }, label)),
    meta("Account", x.account || null),
    connected ? meta("Last sync", x.syncing ? "Syncing now" : x.last_sync_ms ? ago(x.last_sync_ms) : "Never") : null,
    meta("Stored", soon ? null : "On this Mac, scrubbed before it's saved"));

  page.replaceChildren(el("div", { class: "sp" },
    el("button", { class: "btn ghost small sp-back", onclick: () => show("sources") }, icon(["m12 19-7-7 7-7", "M19 12H5"]), "Sources"),
    el("div", { class: "sp-head" }, sourceIcon(x.id, "src-icon xl"),
      el("div", { class: "sp-title" }, el("h1", {}, x.name), el("p", {}, x.description)), actions),
    el("div", { class: "sp-body" }, main, aside)));
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
  else if (r.needs_permission) toast(`${x.name} needs ${x.requirements.find((q) => q.value === r.requirement)?.label || "a Mac permission"}. See its page for the steps.`);
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
  // The dot: gray when off, green when connected, yellow for every on-but-not-connected state.
  off: ["Off", ""], connecting: ["Connecting", "warn"], waiting: ["Waiting for an AI app to connect", "warn"],
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
  loadScrubber();
}

function renderScrubber(r) {
  $("scrub-builtin").replaceChildren(...r.builtin.map((rule) =>
    el("li", {}, el("b", {}, rule.label), el("span", { class: "muted" }, " · always on. " + rule.detail))));
  const rows = r.patterns.length
    ? r.patterns.map((p) => {
        const box = el("input", { type: "checkbox", onchange: (e) => scrubberSet(p.id, e.target.checked) });
        box.checked = !!p.enabled;
        return el("div", { class: "scrub-row" },
          el("label", { class: "check" }, box, el("span", {}, el("b", {}, p.name), " ", el("code", {}, p.pattern))),
          el("button", { class: "btn small", type: "button", onclick: () => scrubberDelete(p.id) }, "Remove"));
      })
    : [el("p", { class: "muted" }, "No extra patterns yet.")];
  $("scrub-patterns").replaceChildren(...rows);
}

async function loadScrubber() {
  const r = await api().scrubber_rules();
  if (!r.ok) return toast(r.error);
  renderScrubber(r);
}

async function scrubberSet(id, enabled) {
  const r = await api().scrubber_set_enabled(id, enabled);
  if (!r.ok) return toast(r.error);
  renderScrubber(r);
}

async function scrubberDelete(id) {
  const ok = await confirmDialog("Remove this pattern? Files read after this won't use it.", "Remove");
  if (!ok) return;
  const r = await api().scrubber_delete(id);
  if (!r.ok) return toast(r.error);
  renderScrubber(r);
}

async function scrubberPreview() {
  const r = await api().scrubber_preview($("scrub-sample").value);
  const box = $("scrub-preview");
  box.hidden = false;
  if (!r.ok) {
    box.replaceChildren(el("p", { class: "error" }, r.error));
    return;
  }
  box.replaceChildren(richText(r.text));
}

// ---------- AI access review ----------
// review.py does the grouping and colours; this draws one tab per app or account. Every colour
// comes with an icon shape and a word, so nothing depends on telling red from green.
const RV_ICON = { red: "circle-x", yellow: "triangle-alert", green: "circle-check", grey: "circle-help" };
const RV_TALLY = [["red", "to fix"], ["yellow", "worth a look"], ["green", "OK"], ["grey", "not checked"]];
const RV_ANSWERS = [["ok", "Looks fine"], ["fixed", "I fixed it"], ["issue", "Found a problem"], ["skip", "Don't use this"]];
const RV_RANK = ["red", "yellow", "grey", "green"];
let rvTab = null;               // selected group id; first visit opens the worst one
const rvOpenRows = new Set();   // checks the user expanded (kept across redraws)
let rvLast = null;

function rvIcon(colour) {
  const i = scIcon(RV_ICON[colour] || RV_ICON.grey, `rv-ic ${colour}`);
  return i;
}

async function loadReview(r) {
  r = r || await api().review();
  if (!r.ok) return toast(r.error);
  rvLast = r;
  const groups = r.groups.filter((g) => g.checks.length || g.id === "claude");
  if (rvTab !== RV_LOG && !groups.some((g) => g.id === rvTab)) {
    const worst = [...groups].sort((a, b) => RV_RANK.indexOf(a.colour) - RV_RANK.indexOf(b.colour))[0];
    rvTab = worst && worst.colour !== "green" ? worst.id : groups[0]?.id;
  }
  $("rv-tally").replaceChildren(...RV_TALLY.filter(([c]) => r.colours[c]).map(([c, word]) =>
    el("span", { class: `rv-t ${c}` }, rvIcon(c), el("b", {}, r.colours[c]), ` ${word}`)));
  $("rv-tabs").replaceChildren(...groups.map((g) => reviewTab(g)), historyTab());
  const g = groups.find((x) => x.id === rvTab);
  $("rv-panel").setAttribute("aria-labelledby", `rv-tab-${rvTab}`);
  if (rvTab === RV_LOG) return loadHistory();
  $("rv-panel").replaceChildren(...(g ? reviewPanel(g, r) : []));
}

// History: SafeContext's own permission log (permlog.py), newest first. Kept only on this Mac.
const RV_LOG = "log";
const RV_LOG_ICON = { approved: ["circle-check", "green"], denied: ["ban", "red"], expired: ["circle-help", "grey"],
  cancelled: ["circle-help", "grey"] };

function historyTab() {
  const sel = rvTab === RV_LOG;
  return el("button", { class: `rv-tab rv-tab-log${sel ? " on" : ""}`, role: "tab", id: `rv-tab-${RV_LOG}`,
      "aria-selected": String(sel), "aria-controls": "rv-panel", tabindex: sel ? "0" : "-1",
      onclick: () => rvSelect(RV_LOG), onkeydown: rvTabKey },
    scIcon("history", "rv-ic grey"), "History");
}

async function loadHistory() {
  const r = await api().permission_log();
  if (!r.ok) return toast(r.error);
  if (rvTab !== RV_LOG) return;
  const intro = el("div", { class: "rv-log-top" },
    el("p", { class: "rv-blurb" }, "Every time an AI app asked to connect through SafeContext, what you answered, " +
      "and changes to your link and settings. Kept only on this Mac, for up to a year."),
    r.entries.length ? el("button", { class: "btn small", onclick: clearHistory }, "Clear") : null);
  if (!r.entries.length) {
    return $("rv-panel").replaceChildren(intro, el("p", { class: "muted rv-log-empty" }, "Nothing yet."));
  }
  let day = "";
  const rows = [];
  for (const e of r.entries) {
    const d = new Date(e.ts_ms);
    const label = d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    if (label !== day) { day = label; rows.push(el("div", { class: "rv-log-day" }, label)); }
    const [icon, colour] = e.kind === "connect" ? (RV_LOG_ICON[e.outcome] || RV_LOG_ICON.expired)
      : e.kind === "scan_change" ? ["triangle-alert", "yellow"] : ["sliders-horizontal", "grey"];
    rows.push(el("div", { class: "rv-log-row" }, scIcon(icon, `rv-ic ${colour}`),
      el("span", { class: "rv-log-text" }, e.text),
      el("time", { class: "muted rv-log-time", datetime: d.toISOString() },
        d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }))));
  }
  $("rv-panel").replaceChildren(intro, el("div", { class: "rv-list rv-log" }, ...rows));
}

async function clearHistory() {
  if (!await confirmDialog("Clear the history? This can't be undone.", "Clear")) return;
  const r = await api().permission_log_clear();
  if (!r.ok) return toast(r.error);
  loadHistory();
}

function reviewTab(g) {
  const open = g.counts.red + g.counts.yellow + g.counts.grey;
  const sel = g.id === rvTab;
  return el("button", { class: `rv-tab${sel ? " on" : ""}${g.skipped ? " skipped" : ""}`, role: "tab", id: `rv-tab-${g.id}`,
      "aria-selected": String(sel), "aria-controls": "rv-panel", tabindex: sel ? "0" : "-1",
      title: `${g.title}: ${g.label}`, onclick: () => rvSelect(g.id), onkeydown: rvTabKey },
    rvIcon(g.colour), g.title,
    open && !g.skipped ? el("span", { class: "rv-n" }, open) : null,
    el("span", { class: "sr" }, ` (${g.label})`));
}

function rvSelect(id, focus = false) {
  rvTab = id;
  loadReview(rvLast);
  if (focus) $(`rv-tab-${id}`)?.focus();
}

function rvTabKey(e) {
  const tabs = [...document.querySelectorAll(".rv-tab")];
  const i = tabs.indexOf(e.currentTarget);
  const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
  if (j == null) return;
  e.preventDefault();
  rvSelect(tabs[(j + tabs.length) % tabs.length].id.replace(/^rv-tab-/, ""), true);
}

function reviewPanel(g, r) {
  const out = [el("p", { class: "rv-blurb" }, g.skipped ? "You said you don't use this." : g.blurb)];
  if (g.id === "claude") out.push(reviewScanRow(r.scan_on));
  out.push(el("div", { class: "rv-list" }, ...g.checks.map(reviewRow)));
  return out;
}

function reviewScanRow(on) {
  const box = el("input", { type: "checkbox", id: "rv-scan" });
  box.checked = on;
  box.addEventListener("change", reviewScan);
  return el("label", { class: "rv-scan-row" }, box,
    el("span", {}, "Also check Claude Desktop and Claude Code on this Mac ",
      el("small", { class: "muted" }, "(names and rules only, never keys or values; nothing leaves this Mac)")));
}

function reviewRow(c) {
  const open = rvOpenRows.has(c.id);
  const bodyId = `rv-b-${c.id}`;
  const toggle = el("button", { class: "rv-line", "aria-expanded": String(open), "aria-controls": bodyId,
      onclick: () => { open ? rvOpenRows.delete(c.id) : rvOpenRows.add(c.id); loadReview(rvLast); } },
    rvIcon(c.colour), el("span", { class: "rv-title" }, c.title),
    el("span", { class: `rv-word ${c.colour}` }, c.label), scIcon("chevron-right", "rv-chev"));
  const row = el("div", { class: `rv-row ${c.colour}${open ? " open" : ""}`, "data-id": c.id },
    el("div", { class: "rv-head" }, toggle,
      c.link ? el("button", { class: "btn small rv-open", title: c.link.startsWith("x-apple") ? "Open System Settings" : "Open page",
        onclick: () => reviewOpen(c.id) }, c.link.startsWith("x-apple") ? "Settings" : "Open", scIcon("external-link"))
        : el("span", { class: "rv-open-gap" })));  // keeps the status words lined up
  if (!open) return row;
  const body = el("div", { class: "rv-body", id: bodyId },
    c.detail ? el("p", { class: "rv-detail" }, c.detail) : null,
    c.items.length ? el("ul", { class: "rv-items" }, ...c.items.map((i) => el("li", {}, i))) : null,
    c.fix ? el("p", { class: "rv-fix" }, el("b", {}, "What to do: "), c.fix) : null);
  if (c.manual) {
    const acts = el("div", { class: "rv-actions", role: "group", "aria-label": `What you found: ${c.title}` });
    for (const [a, text] of RV_ANSWERS) {
      acts.append(el("button", { class: `btn small${c.answer === a ? " chosen" : ""}`, "aria-pressed": String(c.answer === a),
        onclick: () => reviewAnswer(c.id, c.answer === a ? "" : a) }, text));
    }
    if (c.answered_ms) acts.append(el("span", { class: "muted rv-when" }, `Checked ${new Date(c.answered_ms).toLocaleDateString()}`));
    body.append(acts);
  }
  row.append(body);
  return row;
}

async function reviewOpen(id) {
  const r = await api().review_open(id);
  if (!r.ok) toast(r.error);
  else if (!r.opened) toast(state.status && state.status.demo ? "Demo mode doesn't open pages." : "Couldn't open it.");
}

async function reviewAnswer(id, answer) {
  rvOpenRows.add(id);  // keep the row open so the new colour and the chosen answer show together
  await loadReview(await api().review_answer(id, answer));
  document.querySelector(`.rv-row[data-id="${id}"] .rv-actions .btn.chosen`)?.focus({ preventScroll: true });
}

async function reviewScan(e) {
  const r = await api().review_set_scan(e.target.checked);
  loadReview(r);
  if (r.ok && e.target.checked) toast("Checked Claude Desktop and Claude Code.");
}

async function reviewSave() {
  const r = await api().review_save_report();
  if (!r.ok) return toast(r.error);
  toast(r.opened ? "Report saved and opened. Use File → Print to save it as a PDF." : `Report saved to ${r.path}`);
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
  Object.assign(mail, { source: "", rows: [], sel: -1, openKey: null, folders: null });
  $("mail-reader").replaceChildren(el("div", { class: "empty" }, "Select a conversation to read it."));
  mailReaderBar();
  state.shown = false;
  refreshStatus();
}

// ---------- connect ----------
async function connect(btn, errEl) {
  // Sign-in always downloads full messages (scrubbed); reconnecting keeps the account's current mode.
  // Headers-only is still available from the command line: safecontext connect --headers-only.
  const mode = (state.status && state.status.mode) || "full";
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

// ---------- updates (updater.py) ----------
const UPDATE_PHASES = { checking: "Checking…", downloading: "Downloading", verifying: "Checking the signature…",
  installing: "Restarting to finish the update…" };

async function refreshUpdate() {
  const r = await api().update_status();
  if (!r.ok) return;
  state.update = r;
  renderUpdate(r);
}

function renderUpdate(u) {
  $("u-current").textContent = u.current || "—";
  if (u.disabled) {
    $("u-state").textContent = "Updates are off in demo mode.";
    for (const id of ["u-check", "u-install", "u-download", "u-auto", "u-on-quit"]) $(id).disabled = true;
    $("update-banner").hidden = true;
    return;
  }
  const v = u.version;
  const pct = u.phase === "downloading" && u.progress != null ? ` ${Math.round(u.progress * 100)}%` : "";
  const onQuit = !!u.on_quit;  // downloading or staged, to install when the app quits
  // Installing now (Update and restart): keep the banner up with the progress and nothing else to click.
  const working = ["downloading", "verifying", "installing"].includes(u.phase) && !onQuit;
  const checking = u.phase === "checking";
  let text;
  if (onQuit) {
    text = u.phase === "ready" ? `SafeContext ${v} is ready and will be installed when you quit.`
      : u.phase === "verifying" ? `Getting SafeContext ${v} ready: ${UPDATE_PHASES.verifying}`
      : `Getting SafeContext ${v} ready to install when you quit…${pct}`;
  } else if (working || checking) {
    text = UPDATE_PHASES[u.phase] + pct;
  } else if (u.phase === "error") {
    text = u.error;
  } else if (u.available) {
    text = `SafeContext ${v} is available.` + (u.can_install ? "" : ` ${u.cant_install_reason || ""}`);
  } else if (u.checked_ms) {
    text = `You're up to date (checked ${ago(u.checked_ms)}).`;
  } else {
    text = "Not checked yet.";
  }
  if (u.rolled_back && !working) text = `The update to ${u.rolled_back} didn't start, so SafeContext went back to ${u.current}. ${text}`;

  // Settings → Updates
  $("u-state").textContent = text;
  $("u-notes").hidden = !(u.available && u.notes);
  $("u-notes").textContent = u.notes || "";
  $("u-auto").checked = !!u.auto_check;
  $("u-on-quit").checked = !!u.install_on_quit;
  $("u-on-quit").disabled = !u.can_install;
  $("u-check").disabled = working || checking;
  $("u-install").hidden = !(u.available && u.can_install);
  $("u-install").disabled = working || checking;
  $("u-queue").hidden = !(u.available && u.can_install && !onQuit && !working);
  $("u-cancel").hidden = !onQuit;
  $("u-skip").hidden = !(u.available && !u.skipped && !working);
  $("u-download").hidden = !(u.available && !u.can_install);
  $("u-skipped").hidden = !u.skipped_version;
  $("u-skipped-v").textContent = u.skipped_version || "";

  // The banner
  const show = working || (u.available && !u.skipped && !u.later);
  $("update-banner").hidden = !show;
  if (!show) return;
  $("update-banner-text").textContent = working ? text
    : onQuit ? text
    : u.can_install ? `SafeContext ${v} is ready to install.` : `SafeContext ${v} is available.`;
  $("update-whatsnew").hidden = working || !u.notes;
  $("update-notes-text").textContent = u.notes || "";
  if (working || !u.notes) { $("update-notes").hidden = true; $("update-whatsnew").setAttribute("aria-expanded", "false"); }
  $("update-later").hidden = working;
  $("update-cancel").hidden = !onQuit;
  $("update-on-quit").hidden = working || onQuit || !u.can_install;
  $("update-now").textContent = u.can_install ? "Update and restart" : "Download";
  $("update-now").disabled = working || checking;
}

async function updateCall(name, ...args) {
  const r = await api()[name](...args);
  if (r && r.ok === false) toast(r.error);
  refreshUpdate();
}

async function updateInstall() {
  const u = state.update || {};
  if (!u.can_install) { await api().update_open_download(); return; }
  await updateCall("update_install");
}

async function updateCheck() {
  $("u-state").textContent = UPDATE_PHASES.checking;
  await updateCall("update_check");
}

function toggleWhatsNew() {
  const open = $("update-notes").hidden;
  $("update-notes").hidden = !open;
  $("update-whatsnew").setAttribute("aria-expanded", String(open));
}

// ---------- wiring ----------
// ---------- account menu (the email at the bottom of the sidebar) ----------
function acctMenu(open) {
  const menu = $("acct-menu"), btn = $("acct-btn");
  open = open ?? menu.hidden;
  menu.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) menu.querySelector("[role=menuitem]").focus();
}

function wireAcctMenu() {
  const menu = $("acct-menu"), btn = $("acct-btn");
  btn.addEventListener("click", () => acctMenu());
  for (const b of menu.querySelectorAll("[data-menu-view]")) {
    b.addEventListener("click", () => { acctMenu(false); show(b.dataset.menuView); });
  }
  document.addEventListener("click", (e) => {  // click anywhere else closes it
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) acctMenu(false);
  });
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll("[role=menuitem]")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); acctMenu(false); btn.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
  });
}

function wire() {
  wireAcctMenu();
  // Header: hide the sidebar (remembered where storage allows), back / forward through pages.
  let hidden = false;
  try { hidden = localStorage.getItem(SIDEBAR_KEY) === "1"; } catch (e) { /* default: shown */ }
  setSidebarHidden(hidden, false);
  $("nav-sidebar").addEventListener("click", toggleSidebar);
  // The floating sidebar (narrow windows) closes when you pick something, click outside it, or press Esc,
  // and whenever the window gets wide enough to show the sidebar normally.
  document.querySelector(".sidebar").addEventListener("click", (e) => {
    if (e.target.closest(".nav button, .acct-menu button, .sync")) setSidebarOpen(false);
  });
  document.addEventListener("pointerdown", (e) => {
    if ($("shell").classList.contains("sidebar-open") && !e.target.closest(".sidebar, #nav-sidebar")) setSidebarOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("shell").classList.contains("sidebar-open")) setSidebarOpen(false);
  });
  SIDEBAR_COLLAPSE.addEventListener("change", () => setSidebarOpen(false));
  $("nav-back").addEventListener("click", () => navGo(-1));
  $("nav-fwd").addEventListener("click", () => navGo(1));
  document.addEventListener("keydown", (e) => {  // ⌘[ back, ⌘] forward, ⌘\ sidebar (standard Mac shortcuts)
    if (!e.metaKey || e.ctrlKey || e.altKey || document.querySelector("dialog[open]")) return;
    if (e.key === "[") { e.preventDefault(); navGo(-1); }
    else if (e.key === "]") { e.preventDefault(); navGo(1); }
    else if (e.key === "\\") { e.preventDefault(); toggleSidebar(); }
  });
  // Search is in the header: results drop down under it and a page opens only when one is chosen
  // (mail.js searchInit). ⌘K focuses it from anywhere, without changing page.
  document.addEventListener("keydown", (e) => {
    if (!e.metaKey || e.ctrlKey || e.altKey || e.key.toLowerCase() !== "k" || document.querySelector("dialog[open]")) return;
    e.preventDefault();
    const q = $("mail-q");
    q.focus();
    q.select();
  });
  for (const b of document.querySelectorAll(".nav [data-view]")) b.addEventListener("click", () => show(b.dataset.view));
  for (const b of document.querySelectorAll("[data-goto]")) b.addEventListener("click", (e) => { e.preventDefault(); show(b.dataset.goto); });
  $("sync-now").addEventListener("click", async () => { await api().sync_now(); refreshStatus(); });
  $("s-check-now").addEventListener("click", async () => { await api().sync_now(); refreshStatus(); syncRefresh(); });
  // Coming back to the window checks for new mail (the backend skips anything checked in the last 2 min).
  const focused = async () => { if (document.visibilityState === "visible") { await api().window_focused(); refreshStatus(); } };
  window.addEventListener("focus", focused);
  document.addEventListener("visibilitychange", focused);
  $("files-add-folder").addEventListener("click", () => addFiles("folder"));
  $("files-add-file").addEventListener("click", () => addFiles("file"));
  $("ob-connect").addEventListener("click", () => connect($("ob-connect"), $("ob-error")));
  $("reconnect").addEventListener("click", () => connect($("reconnect"), null));
  $("scrub-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const r = await api().scrubber_add($("scrub-name").value, $("scrub-pattern").value);
    if (!r.ok) return toast(r.error);
    $("scrub-name").value = "";
    $("scrub-pattern").value = "";
    renderScrubber(r);
  });
  $("scrub-try").addEventListener("click", scrubberPreview);
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
  $("rv-save").addEventListener("click", reviewSave);
  $("u-check").addEventListener("click", updateCheck);
  $("u-install").addEventListener("click", updateInstall);
  $("update-now").addEventListener("click", updateInstall);
  $("u-download").addEventListener("click", () => api().update_open_download());
  $("update-later").addEventListener("click", () => { $("update-banner").hidden = true; updateCall("update_later"); });
  $("update-whatsnew").addEventListener("click", toggleWhatsNew);
  $("update-on-quit").addEventListener("click", () => updateCall("update_queue"));
  $("u-queue").addEventListener("click", () => updateCall("update_queue"));
  $("update-cancel").addEventListener("click", () => updateCall("update_cancel_queue"));
  $("u-cancel").addEventListener("click", () => updateCall("update_cancel_queue"));
  $("update-skip").addEventListener("click", () => updateCall("update_skip"));
  $("u-skip").addEventListener("click", () => updateCall("update_skip"));
  $("u-unskip").addEventListener("click", (e) => { e.preventDefault(); updateCall("update_unskip"); });
  $("u-auto").addEventListener("change", (e) => updateCall("update_set_auto", e.target.checked));
  $("u-on-quit").addEventListener("change", (e) => updateCall("update_set_on_quit", e.target.checked));
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
  refreshUpdate();
  setInterval(refreshUpdate, 1500);
}

// ---------- window dragging ----------
// A mouse-down on the empty part of the header (.window-drag itself, not a button or the search field in
// it) hands the drag to macOS, which moves the window like a normal title bar (see _start_window_drag in
// main.py). Bound now, not on pywebviewready, so the sign-in strip drags as soon as the page shows.
document.addEventListener("mousedown", (e) => {
  if (e.button !== 0 || e.detail > 1 || !e.target.classList.contains("window-drag")) return;
  const fn = window.pywebview && window.pywebview.api && window.pywebview.api.start_window_drag;
  if (fn) fn();
});

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
