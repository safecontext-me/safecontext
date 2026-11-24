// Mail view: browse synced mail like a mail client and see what wasn't synced.
// Uses app.js helpers (el, richText, fmtDate, senderName, openLink, toast, n, ago, state, api).
// As everywhere in the app, item content only ever goes in as text, never as HTML.
"use strict";

const MAIL_ROW_H = 74;      // must match .mrow height in mail.css
const MAIL_PAGE = 150;
const MAILBOXES = [
  ["all", "All mail", "✉︎"], ["synced", "Synced", "✓"], ["not_synced", "Not synced", "⊘"],
];
const mail = {
  inited: false, view: "all", source: "", query: "", reasons: [], order: "newest",
  rows: [], next: null, loading: false, gen: 0, sel: -1, openKey: null,
  folders: null, coverage: [], reasonList: [], timer: null, lastCovSig: "",
};

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
  let t;
  $("mail-q").addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => { mail.query = $("mail-q").value.trim(); mailReload(); }, 220);
  });
  // Filter menu (components.js): skip reasons only apply to Not synced, so picking one switches to it.
  const filter = $("mail-filter-list");
  $("mail-filter").addEventListener("sc-open", () => { filter.reset(); mailLoadReasons(); });
  filter.addEventListener("sc-filter-change", (e) => {
    const { value, checked } = e.detail;
    mail.reasons = checked ? [...new Set([...mail.reasons, value])] : mail.reasons.filter((g) => g !== value);
    if (mail.view !== "not_synced") mailSetView("not_synced"); else mailReload();
    mailRenderFilter();
  });
  filter.addEventListener("sc-filter-clear", () => { mail.reasons = []; mailReload(); mailRenderFilter(); });
  // Display menu: ordering
  $("mail-order").addEventListener("change", (e) => { mail.order = e.target.value; mailReload(); });
  $("mail-tabs").addEventListener("keydown", (e) => {  // arrow keys move between tabs
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const ids = MAILBOXES.map(([id]) => id);
    const i = ids.indexOf(mail.view) + (e.key === "ArrowRight" ? 1 : -1);
    e.preventDefault();
    e.stopPropagation();
    mailSetView(ids[(i + ids.length) % ids.length]);
    $("mail-tabs").querySelector('[aria-selected="true"]').focus();
  });
  document.addEventListener("keydown", mailKeys);
}

// Called by show("mail") in app.js.
function mailShow() {
  mailInit();
  mailPlaceholder();
  if (!mail.rows.length) mailReload();
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
  const [f, c] = await Promise.all([api().mail_folders(), api().mail_coverage(mail.source)]);
  if (f.ok) { mail.folders = f; mailRenderFolders(); }
  if (c.ok) mail.coverage = c.sources;  // shown in the sync modal now, not on this page
  // Keep the list fresh as the store changes: while a sync fills it (only if the reader is at the
  // top, not mid-scroll or searching), and always on coming back to the page.
  const sig = c.ok ? mailCovSig(c.sources) : "";
  const changed = sig !== mail.lastCovSig;
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

function mailIsMail() {
  const s = mail.source && ((state.status && state.status.sources) || []).find((x) => x.id === mail.source);
  return !s || (s.kinds || []).includes("message");
}

function mailPlaceholder() {
  const q = $("mail-q");
  q.disabled = mail.view === "not_synced";
  q.placeholder = mail.view === "not_synced" ? "Not-synced items have no stored text to search"
    : `Search ${mail.source ? sourceName(mail.source) : "synced mail"}`;
}

function mailBoxLabel(id, label) {
  return id === "all" && !mailIsMail() ? "Everything" : label;
}

function mailRenderFolders() {
  // Tabs above the list. The count for each lives in its hover tooltip, not on the tab.
  const counts = mailCounts();
  const tip = { all: "items in total", synced: "items synced and scrubbed",
    not_synced: "items skipped entirely. Only the date and reason are kept." };
  $("mail-tabs").replaceChildren(...MAILBOXES.map(([id, label]) => {
    const on = mail.view === id;
    return el("button", { class: "mtab" + (on ? " active" : ""), role: "tab", "aria-selected": String(on),
      tabindex: on ? "0" : "-1", title: `${n(counts[id])} ${tip[id]}`, onclick: () => mailSetView(id) },
    mailBoxLabel(id, label));
  }));
}

// ---------- sync modal (opened from the sync card in the sidebar) ----------
// Shows coverage for every source, refreshed every few seconds while it's open.
async function syncModalRefresh() {
  const c = await api().mail_coverage("");
  if (c.ok) { mail.modalCoverage = c.sources; mailRenderCoverage(); }
}

function syncModalOpen() {
  const d = $("sync-modal");
  if (d.open) return;
  syncModalRefresh();
  d.showModal();
  clearInterval(mail.modalTimer);
  mail.modalTimer = setInterval(syncModalRefresh, 3000);
}

function syncModalInit() {
  const card = $("sync"), d = $("sync-modal");
  card.addEventListener("click", (e) => { if (!e.target.closest("#sync-now")) syncModalOpen(); });
  card.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === card) { e.preventDefault(); syncModalOpen(); }
  });
  $("sync-modal-close").addEventListener("click", () => d.close());
  d.addEventListener("click", (e) => { if (e.target === d) d.close(); });  // click on the backdrop
  d.addEventListener("close", () => { clearInterval(mail.modalTimer); mail.modalTimer = null; });
}
syncModalInit();  // this script runs at the end of <body>, so the elements already exist

function mailRenderCoverage() {
  const wrap = $("mail-coverage");
  const list = mail.modalCoverage || [];
  if (!list.length) { wrap.replaceChildren(el("p", { class: "muted" }, "Nothing connected yet.")); return; }
  wrap.replaceChildren(...list.map(mailCoverageCard));
}

// One source in the sync modal: who and when, the counts, progress while syncing, then why things
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
    el("div", { class: "cov-status" }, el("span", { class: "dot" + (status[0] ? ` ${status[0]}` : "") }), status[1]));

  const tile = (num, label, cls = "", title = "") =>
    el("div", { class: "cov-tile", title }, el("div", { class: `cov-num ${cls}`.trim() }, num), el("div", { class: "cov-lbl" }, label));
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
  if (c.last_error) card.append(el("p", { class: "cov-warn" }, c.last_error));

  const reasons = el("div", { class: "bars" });
  bars(reasons, c.dropped_by_group);
  for (const row of reasons.querySelectorAll(".bar-row")) {
    const group = row.firstChild.textContent;
    row.title = `Show “${group}” in Not synced`;
    row.tabIndex = 0;
    const go = () => {  // jump to that source's Not synced list, filtered to this reason
      $("sync-modal").close();
      mail.source = c.id;
      show("mail");
      mail.reasons = [group];
      mailSetView("not_synced");
    };
    row.addEventListener("click", go);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  }
  card.append(el("div", { class: "cov-detail" },
    el("div", { class: "cov-block" }, el("h4", {}, "Not synced, by reason"), reasons),
    el("div", { class: "cov-block" }, el("h4", {}, "What's covered"),
      el("ul", {}, ...c.notes.map((x) => el("li", {}, x)),
        el("li", {}, "Not-synced messages keep only their date and the reason. Their text was never stored.")))));
  return card;
}

async function mailLoadReasons() {
  const r = await api().mail_reasons(mail.source);
  if (!r.ok) return;
  mail.reasonList = r.reasons;
  mailRenderFilter();
}

// The filter menu's categories, what's picked, and the count badge on its button.
function mailRenderFilter() {
  const list = $("mail-filter-list");
  list.categories = [{ id: "reason", label: "Skip reason", hint: "Not synced", icon: "ban",
    options: (mail.reasonList || []).map((x) => ({ value: x.group, label: x.group, count: x.count })) }];
  list.selected = { reason: mail.reasons };
  $("mail-filter").count = mail.reasons.length;
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
async function mailReload(keepSelection = false) {
  const gen = ++mail.gen;
  const prevKey = keepSelection && mail.sel >= 0 && mail.rows[mail.sel] ? mailKey(mail.rows[mail.sel]) : null;
  mail.loading = true;
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, null, MAIL_PAGE, mail.order);
  if (gen !== mail.gen) return;  // a newer request superseded this one
  mail.loading = false;
  mail.rows = r.ok ? r.rows : [];
  mail.next = r.ok ? r.next : null;
  mail.error = r.ok ? "" : r.error;
  mail.sel = prevKey ? mail.rows.findIndex((x) => mailKey(x) === prevKey) : -1;
  if (!keepSelection) $("mail-list").scrollTop = 0;
  mailRender();
}

async function mailMore() {
  if (mail.loading || !mail.next) return;
  const gen = mail.gen;
  mail.loading = true;
  mailRender();
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, mail.next, MAIL_PAGE, mail.order);
  if (gen !== mail.gen) return;
  mail.loading = false;
  if (r.ok) { mail.rows = mail.rows.concat(r.rows); mail.next = r.next; }
  else { mail.next = null; toast(r.error); }
  mailRender();
}

function mailRender() {
  const list = $("mail-list"), spacer = $("mail-spacer");
  const total = mail.rows.length;
  spacer.style.height = `${total * MAIL_ROW_H + (mail.next || mail.loading ? 48 : 0)}px`;
  if (!total) {
    const syncingFirst = mail.coverage.some((c) => !c.first_sync_done);
    spacer.replaceChildren(el("div", { class: "empty" }, mail.loading ? "Loading…" : mail.error ||
      (mail.query ? "No synced mail matches." : mail.view === "not_synced" ? "Nothing was skipped." :
        syncingFirst ? "Your mail is still being downloaded and scrubbed…" : "Nothing here yet.")));
    return;
  }
  const top = list.scrollTop, h = list.clientHeight || 600;
  const first = Math.max(0, Math.floor(top / MAIL_ROW_H) - 8);
  const last = Math.min(total, Math.ceil((top + h) / MAIL_ROW_H) + 8);
  const nodes = [];
  for (let i = first; i < last; i++) nodes.push(mailRow(mail.rows[i], i));
  if (mail.next || mail.loading) {
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
        mailWhen(r.date)),
      el("div", { class: "mrow-subj" }, el("span", {}, ri.label), el("span", { class: "badge skip" }, "⊘ skipped")),
      el("div", { class: "mrow-snip" }, r.thread_title ? ["In “", richText(r.thread_title), "”"] : ri.why));
  }
  const isEvent = r.kind === "event";
  const badges = [];
  if (r.redacted) badges.push(el("span", { class: "badge", title: "Sensitive values blanked out on this Mac" }, `${n(r.redacted)} redacted`));
  if (r.dropped_in_thread) badges.push(el("span", { class: "badge skip", title: "Messages in this thread that weren't synced" }, `${r.dropped_in_thread} not synced`));
  if (r.has_attachments) badges.push(el("span", { class: "badge att", title: "Attachment names only; contents are never downloaded" }, "📎"));
  return el("div", attrs,
    el("div", { class: "mrow-top" },
      el("span", { class: "mrow-from" }, isEvent ? sourceName(r.source) : senderName(r.sender),
        r.count > 1 ? el("span", { class: "cnt" }, String(r.count)) : null),
      isEvent ? el("span", { class: "mrow-date" }, fmtDate(r.date, true)) : mailWhen(r.date)),
    el("div", { class: "mrow-subj" }, el("span", {}, richText(r.title || "(no subject)")), ...badges),
    el("div", { class: "mrow-snip" }, richText((r.snippet || "").replace(/\s+/g, " "))));
}

// The list's date column. Both the date and the time are rendered, stacked in one grid cell
// (so the column is as wide as the longer one and nothing shifts); mail.css shows the time
// while the row is hovered. Events already show their time, so they don't use this.
function mailWhen(iso) {
  if (!iso) return el("span", { class: "mrow-date" }, "");
  const time = new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return el("span", { class: "mrow-date when" },
    el("span", { class: "when-d" }, fmtDate(iso)), el("span", { class: "when-t" }, time));
}

function mailManySources() {
  return !mail.source && !!mail.folders && mail.folders.sources.filter((s) => s.mail).length > 1;
}

function mailSelect(i, { open = false, scroll = true } = {}) {
  if (i < 0 || i >= mail.rows.length) return;
  mail.sel = i;
  if (scroll) {
    const list = $("mail-list");
    const y = i * MAIL_ROW_H;
    if (y < list.scrollTop) list.scrollTop = y;
    else if (y + MAIL_ROW_H > list.scrollTop + list.clientHeight) list.scrollTop = y + MAIL_ROW_H - list.clientHeight;
  }
  mailRender();
  clearTimeout(mail.openTimer);
  if (open) mailOpen(mail.rows[i]);
  else mail.openTimer = setTimeout(() => mailOpen(mail.rows[i], false), 140);  // keyboard: don't fetch every row flown past
}

// ---------- the reader ----------
function mailBackButton() {
  return el("button", { class: "btn small mail-back", onclick: mailCloseReader }, "← Back to list");
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
  if (r.type === "dropped") { reader.replaceChildren(mailBackButton(), mailDroppedDetail(r)); return; }
  const t = await api().mail_thread(r.source, r.thread_id);
  if (mail.openKey !== key) return;
  if (!t.ok) { reader.replaceChildren(mailBackButton(), el("div", { class: "empty" }, t.error)); return; }
  const items = t.entries.filter((e) => e.type === "item");
  const skipped = t.entries.length - items.length;
  const first = items[0] || {};
  const reds = items.reduce((a, m) => a + Object.values(m.redactions || {}).reduce((x, y) => x + y, 0), 0);
  const sub = [`${items.length} ${items.length === 1 ? "message" : "messages"}`];
  if (skipped) sub.push(`${skipped} not synced`);
  if (reds) sub.push(`${n(reds)} ${reds === 1 ? "value" : "values"} redacted`);
  sub.push(sourceName(r.source));
  reader.replaceChildren(mailBackButton(),
    el("h2", { class: "mr-subj" }, richText(first.title || "(no subject)")),
    el("p", { class: "mr-sub" }, sub.join(" · ")),
    ...t.entries.map((e) => e.type === "dropped" ? mailSkippedCard(e) : e.kind === "event" ? eventCard(e) : mailMessageCard(e)));
}

function redactionSummary(red) {
  const entries = Object.entries(red || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return null;
  const total = entries.reduce((a, [, v]) => a + v, 0);
  return el("div", { class: "red-sum", title: "Removed on this Mac before saving" },
    `${n(total)} ${total === 1 ? "item" : "items"} redacted:`,
    ...entries.map(([k, v]) => el("span", { class: "badge" }, `${v} ${REDACTION_NAMES[k] || k.toLowerCase()}`)));
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
        el("div", { class: "msg-meta" }, d.date ? fmtDate(d.date, true) : "Date unknown")),
      d.link ? el("button", { class: "btn small", onclick: () => openLink(d.link) }, "Open in Gmail") : null),
    el("p", { class: "skip-why" }, ri.why),
    el("p", { class: "skip-note" }, "Its sender, subject and text were never stored on this Mac."));
}

function mailDroppedDetail(d) {
  const ri = d.reason_info;
  return el("div", { class: "mr-dropped" },
    el("h2", { class: "mr-subj" }, "Not synced: ", ri.label),
    el("p", { class: "mr-sub" }, sourceName(d.source)),
    el("dl", { class: "details" },
      el("dt", {}, "Date"), el("dd", {}, d.date ? fmtDate(d.date, true) : "Unknown"),
      el("dt", {}, "Why"), el("dd", {}, ri.why || ri.label),
      el("dt", {}, "Rule"), el("dd", {}, el("code", {}, ri.code)),
      d.thread_title ? el("dt", {}, "Thread") : null, d.thread_title ? el("dd", {}, richText(d.thread_title)) : null),
    el("p", { class: "muted" }, "SafeContext skipped this message entirely: only its date and the reason were saved. " +
      "Its sender, subject and text never touched the disk, so they can't be shown here or reach an AI."),
    d.link ? el("button", { class: "btn", onclick: () => openLink(d.link) }, "Open in Gmail") : null);
}

// ---------- keyboard ----------
function mailKeys(e) {
  const q = $("mail-q");
  if (state.view === "mail" && e.metaKey && !e.ctrlKey && !e.altKey && e.key.toLowerCase() === "k"
      && !document.querySelector("dialog[open]")) {  // ⌘K: jump to search
    e.preventDefault();
    if (!q.disabled) { q.focus(); q.select(); }
    return;
  }
  if (state.view !== "mail" || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector("dialog[open]")) return;
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest("sc-menu")) return;  // a menu has the keys
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
  if (e.key === "Escape") {
    if (document.activeElement === q) {
      if (q.value) { q.value = ""; mail.query = ""; mailReload(); }
      q.blur();
      $("mail-list").focus();
    } else if (document.querySelector(".mail-panes.reading")) {
      mailCloseReader();
    }
    return;
  }
  if (typing) {
    if (document.activeElement === q && (e.key === "ArrowDown" || e.key === "Enter") && mail.rows.length) {
      e.preventDefault();
      $("mail-list").focus();
      mailSelect(Math.max(0, mail.sel), { open: e.key === "Enter" });
    }
    return;
  }
  if (e.key === "/") { e.preventDefault(); if (!q.disabled) q.focus(); return; }
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
