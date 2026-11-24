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
  inited: false, view: "all", source: "", query: "", reasons: [],
  rows: [], next: null, loading: false, gen: 0, sel: -1, openKey: null,
  folders: null, coverage: [], covOpen: {}, reasonList: [], timer: null, lastCovSig: "",
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
  $("mail-folder-select").addEventListener("change", (e) => {
    const [kind, value] = e.target.value.split(":");
    if (kind === "view") mailSetView(value); else mailSetSource(value);
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
  if (c.ok) { mail.coverage = c.sources; mailRenderCoverage(); }
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
  const counts = mailCounts();
  const nav = $("mail-folders");
  const box = (id, label, ico) => el("button", {
    class: "mf" + (mail.view === id ? " active" : ""), onclick: () => mailSetView(id),
    title: id === "not_synced" ? "Messages the scrubber skipped entirely. Only the date and reason are kept." : "",
  }, el("span", { class: "mf-ico" + (id === "synced" ? " dot-ok" : id === "not_synced" ? " dot-skip" : "") }, ico),
    el("span", { class: "mf-name" }, mailBoxLabel(id, label)), el("span", { class: "mf-n" }, n(counts[id])));
  nav.replaceChildren(el("div", { class: "mf-k" }, mail.source ? sourceName(mail.source) : "Mailboxes"),
    ...MAILBOXES.map((m) => box(...m)));

  const sel = $("mail-folder-select");
  sel.replaceChildren(...MAILBOXES.map(([id, label]) =>
    el("option", { value: `view:${id}` }, `${mailBoxLabel(id, label)} (${n(counts[id])})`)));
  sel.value = `view:${mail.view}`;
}

function mailRenderCoverage() {
  const wrap = $("mail-coverage");
  wrap.replaceChildren(...mail.coverage.map((c) => {
    const range = c.oldest_ms ? `${monthYear(c.oldest_ms)} – ${monthYear(c.newest_ms)}` : "nothing yet";
    const open = !!mail.covOpen[c.id];
    const line = el("div", { class: "cov-line" },
      el("span", { class: "cov-name" }, SOURCE_ICONS[c.id] || "•", c.name, c.account ? el("small", {}, c.account) : null),
      el("span", { class: "cov-stat", title: "Dates of the oldest and newest message on this Mac" }, range),
      el("span", { class: "cov-stat" }, el("b", { class: "ok" }, n(c.kept)), " synced"),
      el("span", { class: "cov-stat" }, el("b", {}, n(c.dropped)), " not synced"),
      el("span", { class: "cov-stat", title: "Waiting to be re-fetched and re-scrubbed" }, el("b", {}, n(c.pending)), " pending"),
      c.remaining_estimate ? el("span", { class: "cov-stat", title: "Gmail's own count includes Promotions and other mail that's never downloaded, so this is an upper bound" },
        "≤ ", el("b", {}, n(c.remaining_estimate)), " still to fetch") : null,
      el("span", { class: "cov-stat" }, c.syncing ? `Syncing… ${n((c.progress || {}).fetched)} processed`
        : c.last_sync_ms ? `Synced ${ago(c.last_sync_ms)}` : "Never synced"),
      el("button", { class: "cov-toggle", "aria-expanded": String(open),
        onclick: () => { mail.covOpen[c.id] = !open; mailRenderCoverage(); } }, open ? "Hide details" : "Details"));
    const card = el("div", { class: "cov" }, line);
    if (c.syncing || !c.first_sync_done) {
      const total = c.remaining_estimate != null ? c.kept + c.dropped + c.remaining_estimate : 0;
      const bar = el("div", { class: "cov-progress" + (total ? "" : " indeterminate"),
        title: total ? "Share of your mailbox handled so far" : "Syncing. No total from Gmail yet, so there's no percentage." }, el("div"));
      if (total) bar.firstChild.style.width = `${Math.min(100, ((c.kept + c.dropped) / total) * 100)}%`;
      if (c.syncing || total) card.append(bar);
    }
    if (open) {
      const reasons = el("div", { class: "bars" });
      bars(reasons, c.dropped_by_group);
      for (const row of reasons.querySelectorAll(".bar-row")) {
        const group = row.firstChild.textContent;
        row.title = `Show “${group}” in Not synced`;
        row.addEventListener("click", () => { mail.reasons = [group]; mailSetView("not_synced"); });
      }
      const notes = [...c.notes];
      if (c.progress) notes.unshift(`This run: ${n(c.progress.fetched)} processed, ${n(c.progress.kept)} kept, ${n(c.progress.dropped)} skipped` +
        (c.progress.per_min ? ` · ${n(Math.round(c.progress.per_min))}/min` : ""));
      card.append(el("div", { class: "cov-detail" },
        el("div", {}, el("h4", {}, "Not synced, by reason"), reasons),
        el("div", {}, el("h4", {}, "What's covered"),
          el("ul", {}, ...notes.map((x) => el("li", {}, x)),
            el("li", {}, "Not-synced messages keep only their date and the reason. Their text was never stored.")),
          c.last_error ? el("p", { class: "cov-warn" }, c.last_error) : null)));
    }
    return card;
  }));
}

async function mailLoadReasons() {
  const r = await api().mail_reasons(mail.source);
  if (!r.ok) return;
  mail.reasonList = r.reasons;
  const box = $("mail-reasons");
  const total = r.reasons.reduce((a, x) => a + x.count, 0);
  const chip = (label, count, on, click) => el("button", { class: "rchip" + (on ? " on" : ""), onclick: click,
    "aria-pressed": String(on) }, label, el("b", {}, n(count)));
  box.replaceChildren(chip("All reasons", total, !mail.reasons.length, () => { mail.reasons = []; mailReload(); mailLoadReasons(); }),
    ...r.reasons.map((x) => chip(x.group, x.count, mail.reasons.includes(x.group), () => {
      mail.reasons = mail.reasons.includes(x.group) ? mail.reasons.filter((g) => g !== x.group) : [...mail.reasons, x.group];
      mailReload();
      mailLoadReasons();
    })));
}

function mailSetView(view) {
  mail.view = view;
  if (view !== "not_synced") mail.reasons = [];
  $("mail-reasons").hidden = view !== "not_synced";
  mailPlaceholder();
  if (mail.folders) mailRenderFolders();
  if (view === "not_synced") mailLoadReasons();
  mailReload();
}

function mailSetSource(source) {
  mail.source = source;
  if (mail.folders) mailRenderFolders();
  mailRefreshMeta(true);
  mailReload();
}

// ---------- the list (virtualized) ----------
async function mailReload(keepSelection = false) {
  const gen = ++mail.gen;
  const prevKey = keepSelection && mail.sel >= 0 && mail.rows[mail.sel] ? mailKey(mail.rows[mail.sel]) : null;
  mail.loading = true;
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, null, MAIL_PAGE);
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
  const r = await api().mail_list(mail.view, mail.source, mail.query, mail.reasons, mail.next, MAIL_PAGE);
  if (gen !== mail.gen) return;
  mail.loading = false;
  if (r.ok) { mail.rows = mail.rows.concat(r.rows); mail.next = r.next; }
  else { mail.next = null; toast(r.error); }
  mailRender();
}

function mailHead() {
  const what = mailBoxLabel(mail.view, { all: "All mail", synced: "Synced", not_synced: "Not synced" }[mail.view]);
  const where = mail.source ? ` · ${sourceName(mail.source)}` : "";
  const count = `${n(mail.rows.length)}${mail.next ? "+" : ""}`;
  if (mail.query) return `Results for “${mail.query}” in ${what}${where} · ${count} conversations`;
  const unit = mail.view === "not_synced" ? "messages" : mail.view === "synced" ? "conversations" : "rows";
  const reasons = mail.reasons.length ? ` · ${mail.reasons.join(", ")}` : "";
  return `${what}${where}${reasons} · ${count} ${unit} · newest first`;
}

function mailRender() {
  const list = $("mail-list"), spacer = $("mail-spacer");
  $("mail-list-head").textContent = mailHead();
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
  if (state.view !== "mail" || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector("dialog[open]")) return;
  const q = $("mail-q");
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
