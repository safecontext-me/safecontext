// Mail view: browse synced mail like a mail client and see what wasn't synced.
// Uses app.js helpers (el, richText, fmtDate, senderName, openLink, toast, n, ago, state, api).
// As everywhere in the app, item content only ever goes in as text, never as HTML.
"use strict";

const MAIL_ROW_H = 74;      // must match .mrow height in mail.css
const MAIL_PAGE = 150;
const mail = {
  inited: false, view: "all", source: "", query: "", reasons: [], order: "newest",
  rows: [], next: null, loading: false, gen: 0, sel: -1, openKey: null,
  folders: null, coverage: [], reasonList: [], timer: null, lastCovSig: "",
  pages: new Map(),  // first page of each list already seen (source, view, filter, order): shown at once on a switch
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
  $("mail-filter").addEventListener("sc-open", () => { filter.reset(); mailLoadReasons(); });
  filter.addEventListener("sc-filter-change", (e) => {
    const { category, value, checked } = e.detail;
    if (category === "status") {  // one status at a time; unticking it goes back to all mail
      mailSetView(checked ? value : "all");
    } else {  // skip reasons only exist for Not synced, so picking one switches to it
      mail.reasons = checked ? [...new Set([...mail.reasons, value])] : mail.reasons.filter((g) => g !== value);
      if (mail.view !== "not_synced") mailSetView("not_synced"); else mailReload();
    }
    mailRenderFilter();
  });
  filter.addEventListener("sc-filter-clear", () => { mail.reasons = []; mailSetView("all"); mailRenderFilter(); });
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
  const sig = c.ok ? mailCovSig(c.sources) : "";
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
  return s ? { all: s.kept + s.dropped, synced: s.kept, not_synced: s.dropped } : f.mailboxes;
}

function mailPlaceholder() {
  $("mail-q").placeholder = "Search";  // it searches all synced mail, whatever page or tab is showing
}

// The list's title is the source's name; All / Synced / Not synced are a Status filter (mailRenderFilter).
function mailRenderFolders() {
  $("mail-list-title").textContent = mail.source ? sourceName(mail.source) : "All sources";
  mailRenderFilter();
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
// Filter menu: Status (Synced or Not synced; neither = all mail) and, for Not synced, the skip reasons.
function mailRenderFilter() {
  const list = $("mail-filter-list");
  const counts = mail.folders ? mailCounts() : {};
  list.categories = [
    { id: "status", label: "Status", icon: "check",
      options: [{ value: "synced", label: "Synced", count: counts.synced },
        { value: "not_synced", label: "Not synced", count: counts.not_synced }] },
    { id: "reason", label: "Skip reason", hint: "Not synced", icon: "ban",
      options: (mail.reasonList || []).map((x) => ({ value: x.group, label: x.group, count: x.count })) }];
  list.selected = { status: mail.view === "all" ? [] : [mail.view], reason: mail.reasons };
  $("mail-filter").count = (mail.view === "all" ? 0 : 1) + mail.reasons.length;
}

function mailSetView(view) {
  mail.view = view;
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
  return mail.query ? null : JSON.stringify([mail.source, mail.view, mail.reasons, mail.order]);
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
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, null, MAIL_PAGE, mail.order);
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
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, mail.next, MAIL_PAGE, mail.order);
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
      (mail.query ? "No synced mail matches." : mail.view === "not_synced" ? "Nothing was skipped." :
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
}

function mailRow(r, i) {
  const sel = i === mail.sel ? " sel" : "";
  const attrs = { class: `mrow${r.type === "dropped" ? " dropped" : ""}${sel}`, style: `top:${i * MAIL_ROW_H}px`,
    "data-i": String(i), role: "option", "aria-selected": String(i === mail.sel) };
  if (r.type === "dropped") {
    const ri = r.reason_info;
    return el("div", attrs,
      el("div", { class: "mrow-top" },
        el("span", { class: "mrow-from" }, "Not synced", mailManySources() ? el("span", { class: "cnt" }, `· ${sourceName(r.source)}`) : null),
        mailWhen(r)),
      el("div", { class: "mrow-subj" }, el("span", {}, ri.label)),
      el("div", { class: "mrow-bottom" },
        el("span", { class: "mrow-snip" }, r.thread_title ? ["In “", richText(r.thread_title), "”"] : ri.why),
        el("span", { class: "badge badge-secondary" }, "⊘ skipped")));
  }
  const isEvent = r.kind === "event";
  const badges = [];
  if (r.redacted) badges.push(el("span", { class: "badge badge-redacted", title: "Sensitive values blanked out on this Mac" }, `${n(r.redacted)} redacted`));
  if (r.dropped_in_thread) badges.push(el("span", { class: "badge badge-secondary", title: "Messages in this thread that weren't synced" }, `${r.dropped_in_thread} not synced`));
  // Attachment tag hidden for now (design round 2): r.has_attachments still comes from the backend.
  return el("div", attrs,
    el("div", { class: "mrow-top" },
      el("span", { class: "mrow-from" }, isEvent || !r.sender ? sourceName(r.source) : senderName(r.sender),
        r.count > 1 ? el("span", { class: "cnt" }, String(r.count)) : null),
      mailWhen(r, isEvent)),
    el("div", { class: "mrow-subj" }, el("span", {}, richText(r.title || "(no subject)"))),
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
  if (r.type === "dropped") { mailReaderBar(["Not synced: ", r.reason_info.label], r.link, r.source); reader.replaceChildren(mailDroppedDetail(r)); return; }
  const t = await api().mail_thread(r.source, r.thread_id);
  if (mail.openKey !== key) return;
  if (!t.ok) { mailReaderBar(); reader.replaceChildren(el("div", { class: "empty" }, t.error)); return; }
  const items = t.entries.filter((e) => e.type === "item");
  const first = items[0] || {};
  const link = (items.find((m) => m.link) || t.entries.find((e) => e.link) || {}).link || "";  // the thread in Gmail
  mailReaderBar(richText(first.title || "(no subject)"), link, r.source);
  reader.replaceChildren(
    ...t.entries.map((e) => e.type === "dropped" ? mailSkippedCard(e) : e.kind === "event" ? eventCard(e) : mailMessageCard(e)));
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

function mailSkippedCard(d) {
  const ri = d.reason_info;
  return el("article", { class: "msg skipped" },
    el("div", { class: "msg-head" },
      el("div", {},
        el("div", { class: "msg-from" }, "⊘ Not synced · ", ri.label),
        el("div", { class: "msg-meta" }, d.date ? fmtDate(d.date, true) : "Date unknown"))),
    el("p", { class: "skip-why" }, ri.why),
    el("p", { class: "skip-note" }, "Its sender, subject and text were never stored on this Mac."));
}

function mailDroppedDetail(d) {
  const ri = d.reason_info;
  return el("div", { class: "mr-dropped" },
    el("dl", { class: "details" },
      el("dt", {}, "Date"), el("dd", {}, d.date ? fmtDate(d.date, true) : "Unknown"),
      el("dt", {}, "Why"), el("dd", {}, ri.why || ri.label),
      el("dt", {}, "Rule"), el("dd", {}, el("code", {}, ri.code)),
      d.thread_title ? el("dt", {}, "Thread") : null, d.thread_title ? el("dd", {}, richText(d.thread_title)) : null),
    el("p", { class: "muted" }, "SafeContext skipped this message entirely: only its date and the reason were saved. " +
      "Its sender, subject and text never touched the disk, so they can't be shown here or reach an AI."),
    d.link ? el("button", { class: "btn btn-outline", onclick: () => openLink(d.link) }, "Open in Gmail") : null);
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
