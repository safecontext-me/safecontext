// Mail view: browse synced mail like a mail client and see what wasn't synced.
// Uses app.js helpers (el, richText, fmtDate, senderName, openLink, toast, n, ago, state, api).
// As everywhere in the app, item content only ever goes in as text, never as HTML.
"use strict";

const MAIL_ROW_H = 74;      // must match .mrow height in mail.css
const MAIL_PAGE = 150;
const mail = {
  inited: false, view: "all", source: "", query: "", reasons: [], labels: [], order: "newest",
  labelList: [], tagColors: [], thread: null,  // labels and tags for the Filter menu; the thread in the reader
  rows: [], next: null, loading: false, gen: 0, sel: -1, openKey: null,
  folders: null, coverage: [], reasonList: [], timer: null, lastCovSig: "",
  pages: new Map(),  // first page of each list already seen (source, view, filter, order): shown at once on a switch
  // Skipped items, fetched live (see "skipped items" below): previews by "source:id" (null while on its way),
  // rows waiting for one, items already asked for ahead of time. Memory only; forgotten on leaving Mail.
  skip: new Map(), skipWant: new Set(), skipAhead: new Set(), skipTimer: null, skipGen: 0,
};
const MAIL_PAGES_KEPT = 12;

const mailKey = (r) => `${r.type}:${r.source}:${r.type === "dropped" ? r.id : r.thread_id}`;

function mailInit() {
  if (mail.inited) return;
  mail.inited = true;
  const list = $("mail-list");
  list.addEventListener("scroll", () => mailRender());
  list.addEventListener("click", (e) => {
    const row = e.target.closest(".mrow");
    if (row) mailSelect(Number(row.dataset.i), { open: true });
  });
  window.addEventListener("resize", () => state.view === "mail" && mailRender());
  // Filter menu (components.js): skip reasons only apply to Not synced, so picking one switches to it.
  const filter = $("mail-filter-list");
  $("mail-filter").addEventListener("sc-open", () => { filter.reset(); mailLoadReasons(); mailLoadLabels(); });
  filter.addEventListener("sc-filter-change", (e) => {
    const { category, value, checked } = e.detail;
    if (category === "status") {  // one status at a time; unticking it goes back to all mail
      mailSetView(checked ? value : "all");
    } else if (category === "label" || category === "tag") {  // labels and tags: threads with any of them
      const id = Number(value);
      mail.labels = checked ? [...new Set([...mail.labels, id])] : mail.labels.filter((x) => x !== id);
      if (mail.view === "not_synced") mailSetView("all"); else mailReload();  // skipped mail has no labels
    } else {  // skip reasons only exist for Not synced, so picking one switches to it
      mail.reasons = checked ? [...new Set([...mail.reasons, value])] : mail.reasons.filter((g) => g !== value);
      if (mail.view !== "not_synced") mailSetView("not_synced"); else mailReload();
    }
    mailRenderFilter();
  });
  filter.addEventListener("sc-filter-clear", () => { mail.reasons = []; mail.labels = []; mailSetView("all"); mailRenderFilter(); });
  mailTagMenuInit();
  // Display menu: ordering
  $("mail-order").addEventListener("change", (e) => { mail.order = e.target.value; mailReload(); });
  document.addEventListener("keydown", mailKeys);
  $("mail-back").addEventListener("click", mailCloseReader);
  mailResizerInit();
}

// The divider between the message list and the reader: drag it, or focus it and use ← →, to set the list's
// width; double-click resets. The width is remembered for next time where the window allows storage.
function mailResizerInit() {
  const panes = document.querySelector(".mail-panes"), bar = $("mail-resizer");
  const KEY = "safecontext.mailListWidth", MIN = 260, READER_MIN = 320;
  const clamp = (w) => Math.round(Math.max(MIN, Math.min(w, panes.clientWidth - READER_MIN)));
  const current = () => panes.querySelector(".mail-list-wrap").getBoundingClientRect().width;
  const set = (w, save) => {
    w = clamp(w);
    panes.style.setProperty("--mail-list-width", `${w}px`);
    bar.setAttribute("aria-valuenow", String(w));
    if (save) { try { localStorage.setItem(KEY, String(w)); } catch (e) { /* storage unavailable: keep it for this session */ } }
  };
  try { const saved = Number(localStorage.getItem(KEY)); if (saved) panes.style.setProperty("--mail-list-width", `${saved}px`); } catch (e) { /* none saved */ }
  bar.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const left = panes.getBoundingClientRect().left;
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("dragging");
    const move = (ev) => set(ev.clientX - left, false);
    const up = (ev) => {
      bar.classList.remove("dragging");
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      set(ev.clientX - left, true);
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
  });
  bar.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -16, ArrowRight: 16 }[e.key];
    if (!step) return;
    e.preventDefault();
    e.stopPropagation();
    set(current() + step * (e.shiftKey ? 4 : 1), true);
  });
  bar.addEventListener("dblclick", () => {
    panes.style.removeProperty("--mail-list-width");
    try { localStorage.removeItem(KEY); } catch (e) { /* nothing saved */ }
  });
}

// Called by show("mail") in app.js.
function mailShow() {
  mailInit();
  mailPlaceholder();
  if (!mail.rows.length && !mail.loading) mailReload();  // openSource / mailSetView may have started it already
  mailRefreshMeta(true, mail.rows.length > 0);  // coming back: catch up on what synced while away
  clearInterval(mail.timer);
  mail.timer = setInterval(() => {
    if (state.view !== "mail") { clearInterval(mail.timer); mail.timer = null; return; }
    mailRefreshMeta(false);
  }, 3000);
}

// ---------- sidebar, coverage, reasons ----------
// `returning`: the page was just shown again with its old list, so reload it if anything synced
// meanwhile (a background sync can finish while another screen is up).
async function mailRefreshMeta(force, returning = false) {
  const syncing = mail.coverage.some((c) => c.syncing);
  if (!force && !syncing && Date.now() - (mail.metaAt || 0) < 15000) return;
  mail.metaAt = Date.now();
  const source = mail.source;
  const [f, c] = await Promise.all([api().mail_folders(), api().mail_coverage(source)]);
  if (f.ok) { mail.folders = f; mailRenderFolders(); }
  if (source !== mail.source) return;  // switched sources meanwhile: this coverage is for the old one
  if (c.ok) mail.coverage = c.sources;  // shown on the Sync status page, not on this one
  // Keep the list fresh as the store changes: while a sync fills it (only if the reader is at the
  // top, not mid-scroll or searching), and always on coming back to the page. Right after a switch
  // (lastCovSig null) there's nothing to compare with, and the list is being loaded anyway.
  // Read state changes without new mail (read on the phone), so the unread counts are part of it too.
  const sig = c.ok ? mailCovSig(c.sources) + (f.ok ? `|${f.sources.map((x) => x.unread).join()}` : "") : "";
  const changed = mail.lastCovSig !== null && sig !== mail.lastCovSig;
  if (changed && (returning || (!force && $("mail-list").scrollTop < MAIL_ROW_H && !mail.query))) mailReload(true);
  mail.lastCovSig = sig;
  if (mail.view === "not_synced") mailLoadReasons();
}

// Changes whenever a sync stored, dropped or removed something (newest_ms catches one in, one out).
function mailCovSig(sources) {
  return sources.map((x) => `${x.id}:${x.kept}/${x.dropped}/${x.newest_ms || 0}`).join();
}

// Counts for the source picked in the app sidebar (every mail source if none).
function mailCounts() {
  const f = mail.folders;
  const s = mail.source && f.sources.find((x) => x.id === mail.source);
  return s ? { all: s.kept + s.dropped, synced: s.kept, not_synced: s.dropped, unread: s.unread } : f.mailboxes;
}

// Whether the list showing has a read state to filter on (Gmail, iMessage; not Calendar or Dropbox).
function mailHasReadState() {
  const f = mail.folders;
  if (!f) return false;
  const s = mail.source && f.sources.find((x) => x.id === mail.source);
  return s ? !!s.read_state : f.sources.some((x) => x.mail && x.read_state);
}

function mailPlaceholder() {
  $("mail-q").placeholder = "Search";  // it searches all synced mail, whatever page or tab is showing
}

// The list's title is the source's name; All / Synced / Not synced are a Status filter (mailRenderFilter).
function mailRenderFolders() {
  mailRenderFilter();  // it sets the title too
}

// ---------- Sync status page (account menu, or the sidebar sync card) ----------
// Per-source sync details, refreshed every few seconds while the page is open.
async function syncRefresh() {
  const c = await api().mail_coverage("");
  if (c.ok) { mail.syncCoverage = c.sources; mailRenderCoverage(); }
}

function syncPageShow() {
  syncRefresh();
  clearInterval(mail.syncPageTimer);
  mail.syncPageTimer = setInterval(() => {
    if (state.view !== "sync") { clearInterval(mail.syncPageTimer); mail.syncPageTimer = null; return; }
    syncRefresh();
  }, 3000);
}

// The sidebar sync card (shown while syncing or when something needs attention) opens the page.
function syncCardInit() {
  const card = $("sync");
  card.addEventListener("click", (e) => { if (!e.target.closest("#sync-now")) show("sync"); });
  card.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === card) { e.preventDefault(); show("sync"); }
  });
}
syncCardInit();  // this script runs at the end of <body>, so the elements already exist

function mailRenderCoverage() {
  const list = mail.syncCoverage || [];
  $("sync-page-coverage").replaceChildren(...(list.length ? list.map(mailCoverageCard)
    : [el("p", { class: "muted" }, "Nothing connected yet.")]));
}

// One source on the Sync status page: who and when, the counts, progress while syncing, then why things
// were skipped (click a reason to see those items) and what the counts cover. Everything shows at once.
function mailCoverageCard(c) {
  const range = c.oldest_ms ? `${monthYear(c.oldest_ms)} – ${monthYear(c.newest_ms)}` : "Nothing synced yet";
  const status = c.syncing ? ["busy", "Syncing…"] : c.last_error ? ["warn", "Will retry"]
    : !c.first_sync_done ? ["warn", "Paused"] : ["", c.last_sync_ms ? `Synced ${ago(c.last_sync_ms)}` : "Never synced"];

  const head = el("div", { class: "cov-head" },
    sourceIcon(c.id, "cov-ico"),
    el("div", { class: "cov-who" }, el("div", { class: "cov-name" }, c.name),
      el("div", { class: "cov-sub", title: "Dates of the oldest and newest message on this Mac" },
        [c.account, range].filter(Boolean).join(" · "))),
    el("div", { class: "badge badge-outline cov-status" }, el("span", { class: "dot" + (status[0] ? ` ${status[0]}` : "") }), status[1]));

  const tile = (num, label, cls = "", title = "") =>
    el("div", { class: "card card-sm cov-tile", title }, el("div", { class: "card-content" }, el("div", { class: `cov-num ${cls}`.trim() }, num), el("div", { class: "cov-lbl" }, label)));
  const tiles = el("div", { class: "cov-tiles" },
    tile(n(c.kept), "Synced", "ok", "Kept on this Mac, scrubbed"),
    tile(n(c.dropped), "Not synced", "", "Skipped entirely: only the date and reason are kept"),
    tile(n(c.pending), "Pending", "", "Waiting to be re-fetched and re-scrubbed"),
    c.remaining_estimate ? tile(`≤ ${n(c.remaining_estimate)}`, "Still to fetch", "",
      "Gmail's own count includes Promotions and other mail that's never downloaded, so this is an upper bound") : null);

  const card = el("section", { class: "cov" }, head);
  if (c.syncing || !c.first_sync_done) {
    const total = c.remaining_estimate != null ? c.kept + c.dropped + c.remaining_estimate : 0;
    const bar = el("div", { class: "cov-progress" + (total ? "" : " indeterminate"),
      title: total ? "Share of your mailbox handled so far" : "Syncing. No total from Gmail yet, so there's no percentage." }, el("div"));
    if (total) bar.firstChild.style.width = `${Math.min(100, ((c.kept + c.dropped) / total) * 100)}%`;
    const p = c.progress;
    if (c.syncing || total) card.append(el("div", { class: "cov-run" }, bar, p ? el("div", { class: "cov-run-text" },
      `This run: ${n(p.fetched)} processed · ${n(p.kept)} kept · ${n(p.dropped)} skipped` +
      (p.per_min ? ` · ${n(Math.round(p.per_min))}/min` : "")) : null));
  }
  card.append(tiles);
  if (c.last_error) card.append(el("div", { class: "alert alert-destructive", role: "alert" },
    icon("circle-exclamation-point"), el("div", { class: "alert-title" }, c.last_error)));

  const reasons = el("div", { class: "bars" });
  bars(reasons, c.dropped_by_group);
  for (const row of reasons.querySelectorAll(".bar-row")) {
    const group = row.firstChild.textContent;
    row.title = `Show “${group}” in Not synced`;
    row.tabIndex = 0;
    const go = () => {  // jump to that source's Not synced list, filtered to this reason
      mail.source = c.id;
      show("mail");
      mail.reasons = [group];
      mailSetView("not_synced");
    };
    row.addEventListener("click", go);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  }
  card.append(el("div", { class: "cov-detail" },
    el("div", { class: "card card-sm cov-block" }, el("div", { class: "card-header" }, el("div", { class: "card-title" }, "Not synced, by reason")), el("div", { class: "card-content" }, reasons)),
    el("div", { class: "card card-sm cov-block" }, el("div", { class: "card-header" }, el("div", { class: "card-title" }, "What's covered")), el("div", { class: "card-content" },
      el("ul", {}, ...c.notes.map((x) => el("li", {}, x)),
        el("li", {}, "Not-synced messages keep only their date and the reason. Their text was never stored."))))));
  return card;
}

async function mailLoadReasons() {
  const r = await api().mail_reasons(mail.source);
  if (!r.ok) return;
  mail.reasonList = r.reasons;
  mailRenderFilter();
}

// The filter menu's categories, what's picked, and the count badge on its button.
// Filter menu: Status (Unread, Synced or Not synced; none = all mail) and, for Not synced, the skip reasons.
// Unread is as the source shows it (Gmail, Messages); sources without a read state don't offer it.
function mailRenderFilter() {
  const list = $("mail-filter-list");
  const counts = mail.folders ? mailCounts() : {};
  const unread = mailHasReadState() || mail.view === "unread"
    ? [{ value: "unread", label: "Unread", count: counts.unread || 0 }] : [];
  list.categories = [
    { id: "status", label: "Status", icon: "check",
      options: [...unread, { value: "synced", label: "Synced", count: counts.synced },
        { value: "not_synced", label: "Not synced", count: counts.not_synced }] },
    { id: "reason", label: "Skip reason", hint: "Not synced", icon: "ban",
      options: (mail.reasonList || []).map((x) => ({ value: x.group, label: x.group, count: x.count })) },
    { id: "label", label: "Label", hint: "Gmail", icon: "tag",
      options: mail.labelList.filter((l) => l.type === "user" || (l.type === "system" && l.count)).map(labelOption) },
    { id: "tag", label: "Tag", icon: "tag", options: mail.labelList.filter((l) => l.type === "tag").map(labelOption) }];
  const picked = mail.labels.map(String);
  const isTag = new Set(mail.labelList.filter((l) => l.type === "tag").map((l) => String(l.id)));
  list.selected = { status: mail.view === "all" ? [] : [mail.view], reason: mail.reasons,
    label: picked.filter((x) => !isTag.has(x)), tag: picked.filter((x) => isTag.has(x)) };
  $("mail-filter").count = (mail.view === "all" ? 0 : 1) + mail.reasons.length + mail.labels.length;
  // The list's title names the label or tag being shown: "Gmail · Receipts".
  const names = mail.labels.map((id) => (mail.labelList.find((l) => l.id === id) || {}).name).filter(Boolean);
  $("mail-list-title").textContent = (mail.source ? sourceName(mail.source) : "All sources") +
    (names.length ? ` · ${names.length > 1 ? `${names.length} labels` : names[0]}` : "");
}

// Nested Gmail labels ("Work/Clients") read as a path, as in Gmail's sidebar.
const labelOption = (l) => ({ value: String(l.id), label: l.name.split("/").join(" › "), count: l.count });

async function mailLoadLabels() {
  const r = await api().mail_labels(mail.source);
  if (!r.ok) return;
  mail.labelList = r.labels;
  mail.tagColors = r.colors;
  const known = new Set(r.labels.map((l) => l.id));
  if (mail.labels.some((x) => !known.has(x))) { mail.labels = mail.labels.filter((x) => known.has(x)); mailReload(); }
  mailRenderFilter();
}

// ---------- labels and tags ----------
// A small chip: a coloured dot and the name. Gmail labels keep Gmail's colour; tags use the app's tag tokens.
function labelDot(l) {
  const c = l.type === "tag" ? (/^[a-z]+$/.test(l.color) ? `var(--tag-${l.color})` : "var(--tag-gray)")
    : /^#[0-9a-f]{6}$/i.test(l.color) ? l.color : "var(--tag-gray)";
  return el("span", { class: "lbl-dot", style: `background:${c}`, "aria-hidden": "true" });
}

function labelChip(l, extra) {
  const title = l.type === "tag" ? `Tag: ${l.name} (on this Mac only)` : `Gmail label: ${l.name}`;
  return el("span", { class: `lbl-chip${l.type === "tag" ? " tag" : ""}`, title }, labelDot(l), el("span", { class: "lbl-name" }, l.name), extra || null);
}

// On a row: the user's labels and tags (Gmail's own, like Inbox, would be on nearly every row), at most two.
function rowLabels(r) {
  const shown = (r.labels || []).filter((l) => l.type !== "system");
  const starred = (r.labels || []).some((l) => l.type === "system" && l.name === "Starred");
  const out = shown.slice(0, 2).map((l) => labelChip(l));
  if (shown.length > 2) out.push(el("span", { class: "lbl-more", title: shown.slice(2).map((l) => l.name).join(", ") }, `+${shown.length - 2}`));
  if (starred) out.unshift(el("span", { class: "lbl-star", title: "Starred in Gmail" }, icon("star", "sc-svg")));
  return out.length ? el("span", { class: "mrow-labels" }, out) : null;
}

function mailSetView(view) {
  mail.view = view;
  if (view === "synced" || view === "unread") mailForgetSkipped();  // no skipped rows in these lists
  if (view !== "not_synced") mail.reasons = [];
  mailRenderFilter();
  mailPlaceholder();
  if (mail.folders) mailRenderFolders();
  if (view === "not_synced") mailLoadReasons();
  mailReload();
}

// ---------- the list (virtualized) ----------
// Which list is showing. Searches aren't kept: they change with every keystroke.
function mailPageKey() {
  return mail.query ? null : JSON.stringify([mail.source, mail.view, mail.reasons, mail.labels, mail.order]);
}

function mailRemember(key, rows, next) {
  if (!key) return;
  mail.pages.delete(key);  // re-insert so the Map's order is least recently used first
  mail.pages.set(key, { rows, next });
  while (mail.pages.size > MAIL_PAGES_KEPT) mail.pages.delete(mail.pages.keys().next().value);
}

// Load the list's first page. A new list (another source, tab, filter or order) changes on screen right
// away: the first page from last time if there is one, otherwise placeholder rows. The fresh page replaces
// it when it comes. keepSelection: refreshing the same list in place (a sync stored something).
async function mailReload(keepSelection = false) {
  const gen = ++mail.gen;
  const prevKey = keepSelection && mail.sel >= 0 && mail.rows[mail.sel] ? mailKey(mail.rows[mail.sel]) : null;
  const key = mailPageKey();
  mail.loading = true;
  if (!keepSelection) {
    const seen = key && mail.pages.get(key);
    mail.rows = seen ? seen.rows : [];
    mail.next = seen ? seen.next : null;
    mail.sel = -1;
    mail.error = "";
    $("mail-list").scrollTop = 0;
    mailRender();
  }
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, null, MAIL_PAGE, mail.order, mail.labels);
  if (gen !== mail.gen) return;  // a newer request superseded this one
  mail.loading = false;
  mail.rows = r.ok ? r.rows : [];
  mail.next = r.ok ? r.next : null;
  mail.error = r.ok ? "" : r.error;
  if (r.ok) mailRemember(key, r.rows, r.next);
  mail.sel = prevKey ? mail.rows.findIndex((x) => mailKey(x) === prevKey) : -1;
  mailRender();
}

async function mailMore() {
  if (mail.loading || !mail.next) return;  // also waits while a remembered first page is being refreshed
  const gen = mail.gen;
  mail.loading = true;
  mailRender();
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, mail.next, MAIL_PAGE, mail.order, mail.labels);
  if (gen !== mail.gen) return;
  mail.loading = false;
  if (r.ok) { mail.rows = mail.rows.concat(r.rows); mail.next = r.next; }
  else { mail.next = null; toast(r.error, "error"); }
  mailRender();
}

function mailRender() {
  const list = $("mail-list"), spacer = $("mail-spacer");
  const total = mail.rows.length;
  spacer.style.height = `${total * MAIL_ROW_H + (mail.next ? 48 : 0)}px`;
  if (!total && mail.loading) {  // placeholder rows until the first page arrives
    const rows = Math.min(12, Math.ceil((list.clientHeight || 600) / MAIL_ROW_H));
    spacer.style.height = `${rows * MAIL_ROW_H}px`;
    spacer.replaceChildren(...Array.from({ length: rows }, (_, i) => el("div", { class: "mrow skel", style: `top:${i * MAIL_ROW_H}px`, "aria-hidden": "true" },
      el("div", { class: "skel-bar w30" }), el("div", { class: "skel-bar w70" }), el("div", { class: "skel-bar w90" }))));
    return;
  }
  if (!total) {
    const syncingFirst = mail.coverage.some((c) => !c.first_sync_done);
    spacer.replaceChildren(el("div", { class: "empty" }, mail.error ||
      (mail.query ? "No synced mail matches." : mail.view === "unread" ? "Nothing unread." :
        mail.labels.length ? "Nothing synced has that label or tag." :
        mail.view === "not_synced" ? "Nothing was skipped." :
        syncingFirst ? "Your mail is still being downloaded and scrubbed…" : "Nothing here yet.")));
    return;
  }
  const top = list.scrollTop, h = list.clientHeight || 600;
  const first = Math.max(0, Math.floor(top / MAIL_ROW_H) - 8);
  const last = Math.min(total, Math.ceil((top + h) / MAIL_ROW_H) + 8);
  const nodes = [];
  for (let i = first; i < last; i++) nodes.push(mailRow(mail.rows[i], i));
  if (mail.next) {  // more to load (a remembered first page being refreshed has its own `next`)
    nodes.push(el("div", { class: "mrow-loading", style: `top:${total * MAIL_ROW_H}px` }, "Loading more…"));
  }
  spacer.replaceChildren(...nodes);
  if (mail.next && last > total - 40) mailMore();
  mailWantPreviews(first, last);
}

function mailRow(r, i) {
  const sel = i === mail.sel ? " sel" : "";
  const attrs = { class: `mrow${r.type === "dropped" ? " dropped" : ""}${sel}`, style: `top:${i * MAIL_ROW_H}px`,
    "data-i": String(i), role: "option", "aria-selected": String(i === mail.sel) };
  if (r.type === "dropped") {
    const ri = r.reason_info;
    const p = mail.skip.get(skipKey(r));
    const live = p && !p.error && !p.gone;  // sender, subject and snippet fetched just now, never stored
    const many = mailManySources() ? el("span", { class: "cnt" }, `· ${sourceName(r.source)}`) : null;
    const from = live && p.sender && r.source !== "dropbox" ? senderName(p.sender) : live ? sourceName(r.source) : "Not synced";
    const snip = live ? (p.snippet || "") : p && p.gone ? `Deleted from ${sourceName(r.source)} since it was skipped.`
      : r.thread_title ? ["In “", richText(r.thread_title), "”"] : ri.why;
    return el("div", attrs,
      el("div", { class: "mrow-top" }, el("span", { class: "mrow-from" }, from, many), mailWhen(r)),
      el("div", { class: "mrow-subj" }, el("span", {}, live ? (p.title || "(no subject)") : ri.label)),
      el("div", { class: "mrow-bottom" },
        el("span", { class: "mrow-snip" }, snip),
        el("span", { class: "badge badge-secondary", title: `Not synced: ${ri.label}. ${ri.why}` }, "⊘ skipped")));
  }
  const isEvent = r.kind === "event";
  const unread = r.unread > 0;  // a thread is unread while any of its messages is (as Gmail / Messages show it)
  if (unread) attrs.class += " unread";
  const badges = [];
  if (r.redacted) badges.push(el("span", { class: "badge badge-redacted", title: "Sensitive values blanked out on this Mac" }, `${n(r.redacted)} redacted`));
  if (r.dropped_in_thread) badges.push(el("span", { class: "badge badge-secondary", title: "Messages in this thread that weren't synced" }, `${r.dropped_in_thread} not synced`));
  // Attachment tag hidden for now (design round 2): r.has_attachments still comes from the backend.
  return el("div", attrs,
    unread ? el("span", { class: "mrow-dot", title: r.unread > 1 ? `${n(r.unread)} unread` : "Unread" },
      el("span", { class: "mrow-dot-label" }, "Unread")) : null,  // the dot's words, for VoiceOver
    el("div", { class: "mrow-top" },
      el("span", { class: "mrow-from" }, isEvent || !r.sender ? sourceName(r.source) : senderName(r.sender),
        r.count > 1 ? el("span", { class: "cnt" }, String(r.count)) : null),
      mailWhen(r, isEvent)),
    el("div", { class: "mrow-subj" }, el("span", {}, richText(r.title || "(no subject)")), rowLabels(r)),
    el("div", { class: "mrow-bottom" },
      el("span", { class: "mrow-snip" }, richText((r.snippet || "").replace(/\s+/g, " "))), ...badges));
}

// The list's date column, as in Gmail: the time for today's mail ("2:15 PM"), otherwise the
// short date ("Nov 24"); events always show date and time. The full date and time is a native
// tooltip in Gmail's hover format, e.g. "Wed, Mar 4, 2026, 3:42 PM", which WebKit shows after
// its own hover delay. The Mac's locale decides 12- or 24-hour time. All-day events show
// their day (read in UTC, as in the event view) and no time.
function mailWhen(r, withTime = false) {
  const iso = r.date;
  if (!iso) return el("span", { class: "mrow-date" }, "");
  if (r.all_day) return el("span", { class: "mrow-date", title: mailDateTitle(r) }, fmtAllDay(iso));
  const d = new Date(iso);
  const text = !withTime && d.toDateString() === new Date().toDateString()
    ? d.toLocaleString(undefined, { hour: "numeric", minute: "2-digit" })
    : fmtDate(iso, withTime);
  return el("span", { class: "mrow-date", title: mailDateTitle(r) }, text);
}

function mailDateTitle(r) {
  if (r.all_day) {
    const last = allDayLast(r.date, r.end);
    return fmtAllDay(r.date, true) + (last ? ` – ${fmtAllDay(last, true)}` : "");
  }
  return new Date(r.date).toLocaleString(undefined, { weekday: "short", year: "numeric", month: "short",
    day: "numeric", hour: "numeric", minute: "2-digit" });
}

function mailManySources() {
  return !mail.source && !!mail.folders && mail.folders.sources.filter((s) => s.mail).length > 1;
}

function mailSelect(i, { open = false, scroll = true } = {}) {
  if (i < 0 || i >= mail.rows.length) return;
  mail.sel = i;
  if (scroll) {
    const list = $("mail-list");
    const y = i * MAIL_ROW_H + ($("mail-spacer").offsetTop || 0);  // rows start below the list's top inset
    if (y < list.scrollTop) list.scrollTop = y;
    else if (y + MAIL_ROW_H > list.scrollTop + list.clientHeight) list.scrollTop = y + MAIL_ROW_H - list.clientHeight;
  }
  mailRender();
  clearTimeout(mail.openTimer);
  if (open) mailOpen(mail.rows[i]);
  else mail.openTimer = setTimeout(() => mailOpen(mail.rows[i], false), 140);  // keyboard: don't fetch every row flown past
}

// ---------- header search ----------
// Typing shows matching mail in a dropdown under the field; nothing changes page until a result is
// chosen. "Show all results" filters the mail list instead. Works from any page.
const search = { results: [], active: -1, gen: 0, timer: null, text: "" };
const SEARCH_LIMIT = 8;

function searchInit() {
  const q = $("mail-q"), box = $("search-results");
  q.addEventListener("input", () => {
    clearTimeout(search.timer);
    const text = q.value.trim();
    if (!text) { searchClose(); return; }
    search.timer = setTimeout(() => searchRun(text), 150);
  });
  q.addEventListener("keydown", searchKeys);
  q.addEventListener("focus", () => { if (q.value.trim() && search.text === q.value.trim()) box.hidden = false; });
  q.addEventListener("blur", () => setTimeout(searchClose, 150));  // let a click on a result land first
  box.addEventListener("mousedown", (e) => e.preventDefault());    // keep focus in the field while clicking
}
searchInit();  // this script runs at the end of <body>, so the elements already exist

async function searchRun(text) {
  const gen = ++search.gen;
  const r = await api().mail_list("all", "", text, null, null, SEARCH_LIMIT, "newest");
  if (gen !== search.gen) return;  // a newer search started meanwhile
  search.text = text;
  search.results = r.ok ? r.rows : [];
  search.active = search.results.length ? 0 : -1;
  searchRender(r.ok ? null : r.error);
}

function searchRender(error) {
  const box = $("search-results");
  const items = search.results.map((r, i) => el("button", {
    class: "sr-item" + (i === search.active ? " active" : ""), type: "button", role: "option",
    "aria-selected": String(i === search.active), onclick: () => searchChoose(i) },
  sourceIcon(r.source, "sr-ico"),
  el("span", { class: "sr-main" },
    el("span", { class: "sr-title" }, richText(r.title || "(no subject)")),
    el("span", { class: "sr-meta" }, r.kind === "event" || !r.sender ? sourceName(r.source) : senderName(r.sender), " · ", r.all_day ? fmtAllDay(r.date) : fmtDate(r.date)))));
  const all = search.results.length;
  const footer = el("button", { class: "sr-item sr-all" + (search.active === all ? " active" : ""), type: "button",
    role: "option", "aria-selected": String(search.active === all), onclick: () => searchChoose(all) },
  el("span", { class: "sr-main" }, `Show all results for “${search.text}”`));
  box.replaceChildren(...(error ? [el("div", { class: "sr-empty" }, error)]
    : items.length ? items : [el("div", { class: "sr-empty" }, "No mail matches.")]), footer);
  box.hidden = false;
  const active = box.querySelector(".active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function searchClose() {
  $("search-results").hidden = true;
}

function searchKeys(e) {
  const box = $("search-results"), q = $("mail-q");
  const count = search.results.length + 1;  // results plus "Show all results"
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (box.hidden || !search.text) return;
    e.preventDefault();
    search.active = (search.active + (e.key === "ArrowDown" ? 1 : -1) + count) % count;
    searchRender();
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (!q.value.trim()) return;
    if (search.text !== q.value.trim()) { clearTimeout(search.timer); searchRun(q.value.trim()).then(() => searchChoose(search.active)); return; }
    searchChoose(search.active < 0 ? search.results.length : search.active);
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    if (!box.hidden) { searchClose(); return; }
    q.value = "";
    if (mail.query) { mail.query = ""; mailReload(); }  // clear a "Show all results" filter too
    q.blur();
  }
}

// Open the chosen result (or the filtered list for "Show all results"): only now does the page change.
function searchChoose(i) {
  const q = $("mail-q");
  searchClose();
  if (i >= search.results.length) {  // Show all results: filter the mail list by the search
    mailInit();
    if (state.view !== "mail") show("mail");
    mail.query = search.text;
    if (mail.view === "not_synced") mailSetView("all"); else mailReload();
    q.blur();
    return;
  }
  const r = search.results[i];
  q.value = "";
  q.blur();
  search.text = "";
  if (mail.query) mail.query = "";
  if (mail.source !== r.source) openSource(r.source); else { mailInit(); show("mail"); mailReload(); }
  mailOpen(r, true);
}

// ---------- the reader ----------
// Its title sits in the bar above it (same height as the tab row), with "Open in Gmail" (or Calendar) on the
// right; the body scrolls below. mailReaderBar() with no title clears both.
function mailReaderBar(title, link = "", source = "") {
  $("mail-reader-title").replaceChildren(...(title ? [].concat(title) : []));
  const btn = $("mail-open-orig");
  btn.hidden = !link;
  btn.onclick = link ? () => openLink(link) : null;
  btn.replaceChildren(...(link ? [sourceIcon(source, "btn-ico"), OPEN_LABELS[source] || "Open original"] : []));
}

function mailCloseReader() {
  document.querySelector(".mail-panes").classList.remove("reading");
  $("mail-list").focus();
}

async function mailOpen(r, show = true) {
  if (!r) return;
  const key = mailKey(r);
  if (show) document.querySelector(".mail-panes").classList.add("reading");
  if (mail.openKey === key) return;
  mail.openKey = key;
  const reader = $("mail-reader");
  reader.scrollTop = 0;
  mail.thread = null;
  $("mail-tag-menu").hidden = true;
  if (r.type === "dropped") { mailOpenSkipped(r, key); return; }
  const t = await api().mail_thread(r.source, r.thread_id);
  if (mail.openKey !== key) return;
  if (!t.ok) { mailReaderBar(); reader.replaceChildren(el("div", { class: "empty" }, t.error)); return; }
  mail.thread = { source: r.source, thread_id: r.thread_id, labels: t.labels || [] };
  $("mail-tag-menu").hidden = false;
  const items = t.entries.filter((e) => e.type === "item");
  const first = items[0] || {};
  const link = (items.find((m) => m.link) || t.entries.find((e) => e.link) || {}).link || "";  // the thread in Gmail
  mailReaderBar(richText(first.title || "(no subject)"), link, r.source);
  reader.replaceChildren(el("div", { class: "mr-labels", id: "mail-reader-labels" }),
    ...t.entries.map((e) => e.type === "dropped" ? mailSkippedCard(e) : e.kind === "event" ? eventCard(e) : mailMessageCard(e)));
  mailRenderThreadLabels();
}

// The open thread's labels and tags, above its first message. A tag's × takes it off.
function mailRenderThreadLabels() {
  const box = $("mail-reader-labels");
  if (!box || !mail.thread) return;
  box.replaceChildren(...mail.thread.labels.map((l) => l.type !== "tag" ? labelChip(l)
    : labelChip(l, el("button", { class: "lbl-x", type: "button", "aria-label": `Remove tag ${l.name}`, title: "Remove tag",
      onclick: () => mailToggleTag(l.id, false) }, icon("x", "sc-svg")))));
}

// ---------- the reader's Tags menu: tick tags on or off for the open thread, or make a new one ----------
function mailTagMenuInit() {
  const menu = $("mail-tag-menu");
  // Shown at once from the tags already known (so typing goes straight into "New tag…"), then refreshed.
  menu.addEventListener("sc-open", async () => { mailRenderTagMenu(true); await mailLoadLabels(); if (menu.open) mailRenderTagMenu(true, true); });
}

// keepTyped: a refresh while the menu is open keeps what's being typed in "New tag…".
function mailRenderTagMenu(focusNew = false, keepTyped = false) {
  const panel = $("mail-tag-panel");
  if (!mail.thread) { panel.replaceChildren(); return; }
  const typed = keepTyped ? (panel.querySelector(".sc-filter-q") || {}).value || "" : "";
  const on = new Set(mail.thread.labels.filter((l) => l.type === "tag").map((l) => l.id));
  const tags = mail.labelList.filter((l) => l.type === "tag");
  const input = el("input", { class: "sc-filter-q", type: "text", placeholder: "New tag…", "aria-label": "New tag name",
    autocomplete: "off", spellcheck: "false", maxlength: "60" });
  input.value = typed;
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter" || !input.value.trim()) return;
    e.preventDefault();
    const r = await api().tag_create(input.value.trim(), "");
    if (!r.ok) { toast(r.error, "error"); return; }
    await mailToggleTag(r.tag.id, true);
    await mailLoadLabels();
    mailRenderTagMenu(true);
  });
  panel.replaceChildren(
    el("div", { class: "sc-filter-head" }, input),
    el("div", { class: "sc-filter-items", role: "menu" }, ...(tags.length ? tags.map((l) =>
      el("button", { class: "dropdown-menu-item sc-filter-opt", type: "button", role: "menuitemcheckbox", "aria-checked": String(on.has(l.id)),
        onclick: () => mailToggleTag(l.id, !on.has(l.id)).then(() => mailRenderTagMenu()) },
      el("span", { class: "sc-check" + (on.has(l.id) ? " on" : "") }, on.has(l.id) ? icon("check", "sc-svg") : null),
      labelDot(l), el("span", { class: "sc-filter-label" }, l.name)))
      : [el("div", { class: "sc-empty" }, "No tags yet. Type a name and press Return.")])),
    el("p", { class: "tag-menu-note" }, "Tags stay on this Mac. Gmail never sees them."));
  if (focusNew) input.focus();
}

async function mailToggleTag(id, on) {
  const t = mail.thread;
  if (!t) return;
  const r = await api().tag_thread(id, t.source, t.thread_id, on);
  if (!r.ok) { toast(r.error, "error"); return; }
  t.labels = r.labels;
  mailRenderThreadLabels();
  // the row in the list shows tags too: update it in place
  const row = mail.rows.find((x) => x.type === "thread" && x.source === t.source && x.thread_id === t.thread_id);
  if (row) { row.labels = r.labels; mailRender(); }
  mail.pages.clear();  // remembered pages may show the old tags
}

function redactionSummary(red) {
  const entries = Object.entries(red || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return null;
  const total = entries.reduce((a, [, v]) => a + v, 0);
  return el("div", { class: "red-sum", title: "Removed on this Mac before saving" },
    `${n(total)} ${total === 1 ? "item" : "items"} redacted:`,
    ...entries.map(([k, v]) => el("span", { class: "badge badge-redacted" }, `${v} ${REDACTION_NAMES[k] || k.toLowerCase()}`)));
}

function mailMessageCard(m) {
  const card = messageCard({ ...m, recipients: m.participants });
  const head = card.querySelector(".msg-head");
  const sum = redactionSummary(m.redactions);
  if (sum) head.after(sum);
  return card;
}

// A skipped message inside a synced thread: its reason, and a button that fetches it to show here.
function mailSkippedCard(d) {
  const ri = d.reason_info;
  const card = el("article", { class: "msg skipped" },
    el("div", { class: "msg-head" },
      el("div", {},
        el("div", { class: "msg-from" }, "⊘ Not synced · ", ri.label),
        el("div", { class: "msg-meta" }, d.date ? fmtDate(d.date, true) : "Date unknown"))),
    el("p", { class: "skip-why" }, ri.why),
    el("p", { class: "skip-note" }, "Its sender, subject and text were never stored on this Mac."),
    el("div", { class: "skip-actions" },
      el("button", { class: "btn btn-outline btn-sm", onclick: async (e) => {
        e.currentTarget.disabled = true;
        e.currentTarget.textContent = `Fetching from ${sourceName(d.source)}…`;
        const res = await api().skipped_item(d.source, d.id);
        card.replaceChildren(...skippedView(d, res, true));
      } }, icon("eye", "btn-ico"), "Show it")));
  return card;
}

// ---------- skipped items, fetched live ----------
// A skipped item has no stored content: only its date and the reason. While a list with skipped rows is on
// screen, the rows in view ask their source for sender, subject and a snippet; opening one fetches it whole
// and fetches the next few ahead, so j / k feel instant. The backend (app/skipped.py) holds what it fetches
// in memory only: never stored, never searchable, never reachable by an AI app. Leaving Mail forgets it all.
const skipKey = (r) => `${r.source}:${r.id}`;

function mailWantPreviews(first, last) {
  for (let i = first; i < last; i++) {
    const r = mail.rows[i];
    if (r && r.type === "dropped" && !mail.skip.has(skipKey(r))) mail.skipWant.add(skipKey(r));
  }
  if (mail.skipWant.size && !mail.skipTimer) mail.skipTimer = setTimeout(mailFetchPreviews, 120);  // after a scroll settles
}

async function mailFetchPreviews() {
  const keys = [...mail.skipWant].slice(0, 40);
  for (const k of keys) { mail.skipWant.delete(k); mail.skip.set(k, null); }
  const gen = mail.skipGen;
  const rows = keys.map((k) => { const at = k.indexOf(":"); return { source: k.slice(0, at), id: k.slice(at + 1) }; });
  const r = await api().skipped_previews(rows);
  if (gen !== mail.skipGen) return;  // forgotten meanwhile
  for (const k of keys) mail.skip.set(k, r.ok ? (r.previews[k] || { error: "" }) : { error: r.error });
  mail.skipTimer = null;
  mailRender();  // may queue the rows that scrolled into view meanwhile
}

// Leaving Mail (or a list without skipped rows): drop every skipped item held here and in the backend.
function mailForgetSkipped() {
  if (!mail.skip.size && !mail.skipAhead.size && !mail.skipWant.size) return;
  mail.skip.clear();
  mail.skipWant.clear();
  mail.skipAhead.clear();
  mail.skipGen++;
  clearTimeout(mail.skipTimer);
  mail.skipTimer = null;
  api().skipped_forget();
}

// The next few skipped rows below the open one, fetched now so opening them is instant.
function mailPrefetchSkipped() {
  let left = 3;
  for (let i = mail.sel + 1; i < mail.rows.length && left > 0 && i < mail.sel + 30; i++) {
    const r = mail.rows[i];
    if (r.type !== "dropped") continue;
    left--;
    const k = skipKey(r);
    if (mail.skipAhead.has(k)) continue;
    mail.skipAhead.add(k);
    api().skipped_item(r.source, r.id);  // the backend keeps it in memory for when it's opened
  }
}

const skippedTitle = (title) => [el("span", { class: "badge badge-warning mr-skip-badge" }, icon("ban", "sc-svg"), "Skipped"), " ", richText(title)];

async function mailOpenSkipped(r, key) {
  const reader = $("mail-reader");
  const p = mail.skip.get(skipKey(r));
  mailReaderBar(skippedTitle(p && !p.error && !p.gone && p.title ? p.title : r.reason_info.label), r.link, r.source);
  reader.replaceChildren(skippedBanner(r, ""), el("div", { class: "skip-loading", "aria-label": `Fetching it from ${sourceName(r.source)}` },
    el("div", { class: "skel-bar w30" }), el("div", { class: "skel-bar w70" }), el("div", { class: "skel-bar w90" }), el("div", { class: "skel-bar w70" })));
  const res = await api().skipped_item(r.source, r.id);
  if (mail.openKey !== key) return;
  mail.skipAhead.add(skipKey(r));
  const it = res.ok && res.item;
  if (it) {
    mail.skip.set(skipKey(r), { sender: it.sender, title: it.title, snippet: (it.body || "").replace(/\s+/g, " ").slice(0, 200) });
    mailReaderBar(skippedTitle(it.title || "(no subject)"), it.link || r.link, r.source);
  }
  reader.replaceChildren(...skippedView(r, res));
  mailPrefetchSkipped();
}

// The banner on a skipped item: what it is, and why it was skipped.
function skippedBanner(d, fetchedFrom) {
  const ri = d.reason_info;
  return el("div", { class: "alert alert-warning skip-banner", role: "note" },
    icon("ban"),
    el("div", { class: "alert-title" }, "Skipped: not saved on this Mac, and AI apps can't see it"),
    el("div", { class: "alert-description" },
      el("p", {}, fetchedFrom ? `${d.source === "imessage" ? "Read from Messages on this Mac" : `Fetched from ${fetchedFrom}`} just now so you can read it. ` +
          "It isn't kept: SafeContext forgets it when you leave Mail."
        : `Only its date and the reason were saved. Opening it fetches it from ${sourceName(d.source)} to show you, without saving it.`),
      el("details", { class: "skip-why-more" },
        el("summary", {}, "Why was this skipped?"),
        el("p", {}, ri.why || ri.label),
        el("p", { class: "muted" }, "Rule: ", el("code", {}, ri.code), ". SafeContext skips items like this whole, " +
          "rather than storing a scrubbed copy, so nothing from them can reach an AI app."))));
}

// What the reader shows for a skipped item: the banner, then the item as fetched (or why it couldn't be).
function skippedView(d, res, inThread = false) {
  const it = res.ok && res.item;
  const banner = skippedBanner(d, it ? sourceName(d.source) : "");
  if (!it) {
    const msg = res.ok ? res.error : res.error || "Couldn't fetch it.";
    return [banner, el("div", { class: "alert alert-destructive skip-error" }, icon("circle-exclamation-point"),
        el("div", { class: "alert-title" }, msg),
        res.needs_reconnect ? el("div", { class: "alert-description" }, "Open Sources to reconnect it.") :
        res.offline ? el("div", { class: "alert-description" }, "Skipped items aren't saved, so they can only be shown while you're online.") : null),
      el("dl", { class: "details" },
        el("dt", {}, "Date"), el("dd", {}, d.date ? fmtDate(d.date, true) : "Unknown"),
        el("dt", {}, "Why"), el("dd", {}, d.reason_info.why || d.reason_info.label),
        d.thread_title ? el("dt", {}, "Thread") : null, d.thread_title ? el("dd", {}, richText(d.thread_title)) : null),
      !inThread && d.link ? el("button", { class: "btn btn-outline", onclick: () => openLink(d.link) }, sourceIcon(d.source, "btn-ico"), OPEN_LABELS[d.source] || "Open original") : null].filter(Boolean);
  }
  const m = { ...it, source: d.source };
  const card = it.kind === "event" ? eventCard(m) : messageCard(m);
  card.classList.add("skip-content");
  if (it.note && !it.body) card.querySelector(".msg-body")?.remove();  // a file that can't be shown: the note says so
  if (inThread && it.title) card.querySelector(".msg-head").prepend(el("div", { class: "skip-subject" }, richText(it.title)));
  return [banner, card,
    it.note ? el("p", { class: "muted skip-note" }, it.note) : null,
    inThread && it.link ? el("button", { class: "btn btn-outline btn-sm", onclick: () => openLink(it.link) }, sourceIcon(d.source, "btn-ico"), OPEN_LABELS[d.source] || "Open original") : null].filter(Boolean);
}

// ---------- keyboard ----------
function mailKeys(e) {
  const q = $("mail-q");  // ⌘K is handled app-wide in app.js (search is in the header)
  if (state.view !== "mail" || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector("dialog[open]")) return;
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest("sc-menu")) return;  // a menu has the keys
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
  if (typing) return;  // the search field's own keys are handled by searchKeys
  if (e.key === "Escape") {
    if (document.querySelector(".mail-panes.reading")) mailCloseReader();
    return;
  }
  if (e.key === "/") { e.preventDefault(); q.focus(); return; }
  if (e.key === "f" || e.key === "F") { e.preventDefault(); $("mail-filter").show(); return; }
  if ((e.key === "t" || e.key === "T") && !$("mail-tag-menu").hidden) { e.preventDefault(); $("mail-tag-menu").show(); return; }
  const inReader = $("mail-reader").contains(document.activeElement);
  const step = { j: 1, k: -1, ...(inReader ? {} : { ArrowDown: 1, ArrowUp: -1 }) }[e.key];  // arrows scroll the reader
  if (step) {
    e.preventDefault();
    $("mail-list").focus({ preventScroll: true });
    mailSelect(mail.sel < 0 ? 0 : Math.min(mail.rows.length - 1, Math.max(0, mail.sel + step)));
  } else if (e.key === "Enter" && mail.sel >= 0) {
    e.preventDefault();
    mailOpen(mail.rows[mail.sel], true);
    $("mail-reader").focus({ preventScroll: true });
  }
}

// ---------- Settings › Tags: rename, recolour or delete the user's tags ----------
async function loadTagSettings() {
  const r = await api().mail_labels("");
  const box = $("s-tag-list");
  if (!r.ok) { box.replaceChildren(el("p", { class: "muted" }, r.error)); return; }
  mail.tagColors = r.colors;
  const tags = r.labels.filter((l) => l.type === "tag");
  if (!tags.length) { box.replaceChildren(el("p", { class: "muted tag-admin-empty" }, "No tags yet.")); return; }
  box.replaceChildren(...tags.map(tagAdminRow));
}

function tagAdminRow(l) {
  const save = async (change) => {
    const r = await api().tag_update(l.id, change.name ?? null, change.color ?? null);
    if (!r.ok) toast(r.error, "error");
    mail.pages.clear();
    loadTagSettings();
  };
  const name = el("input", { class: "input tag-admin-name", type: "text", value: l.name, maxlength: "60",
    "aria-label": `Tag name: ${l.name}` });
  name.addEventListener("change", () => { if (name.value.trim() && name.value.trim() !== l.name) save({ name: name.value.trim() }); else name.value = l.name; });
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") name.blur(); if (e.key === "Escape") { name.value = l.name; name.blur(); } });
  const colors = el("div", { class: "tag-swatches", role: "radiogroup", "aria-label": `Colour for ${l.name}` },
    ...mail.tagColors.map((c) => el("button", { class: `tag-swatch${c === l.color ? " on" : ""}`, type: "button", role: "radio",
      "aria-checked": String(c === l.color), "aria-label": c, title: c[0].toUpperCase() + c.slice(1),
      style: `background:var(--tag-${c})`, onclick: () => c !== l.color && save({ color: c }) })));
  const del = el("button", { class: "btn btn-ghost btn-sm", type: "button", "data-variant": "destructive",
    onclick: async () => {
      const where = l.count === 1 ? "1 conversation" : `${n(l.count)} conversations`;
      if (!await confirmDialog(`“${l.name}” comes off ${where}. The conversations themselves stay.`, "Delete tag")) return;
      const r = await api().tag_delete(l.id);
      if (!r.ok) toast(r.error, "error");
      mail.labels = mail.labels.filter((x) => x !== l.id);
      mail.pages.clear();
      loadTagSettings();
    } }, "Delete");
  return el("div", { class: "tag-admin-row" }, labelDot(l), name,
    el("span", { class: "muted tag-admin-count" }, l.count === 1 ? "1 conversation" : `${n(l.count)} conversations`), colors, del);
}
