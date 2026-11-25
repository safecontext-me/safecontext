// Small UI components as native Web Components (custom elements). No library and no build step.
// They render into the light DOM, so app.css / components.css theme them like the rest of the app.
// As everywhere in the app, text only ever goes in as text, never as HTML.
//
//   <sc-menu icon="…" label="…" hotkey="F">   an icon button that opens a popover with its children
//   <sc-filter-list>                          "Add filter…" search, categories, and a flyout of options
"use strict";

function scEl(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v != null && v !== false) n.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}


// ---------- <sc-menu icon="…" label="…" hotkey="F"> ----------
// The button is a ghost icon Button (shadcn.css), 28px; size="icon" makes it 32px to sit beside a default Button.
// align="end" opens the panel from the right edge; compact makes it fit a short list of .sc-item actions.
// An icon button that toggles a popover holding the element's original children.
// Closes on outside click, Esc, or another menu opening. Fires "sc-open" / "sc-close".
// `.count = n` shows a small badge on the button (e.g. how many filters are on).
class ScMenu extends HTMLElement {
  connectedCallback() {
    if (this._panel) return;
    const label = this.getAttribute("label") || "";
    const hotkey = this.getAttribute("hotkey");
    this._panel = scEl("div", { class: "dropdown-menu sc-menu-panel", role: "dialog", "aria-label": label, hidden: "" },
      ...this.childNodes);
    this._badge = scEl("span", { class: "sc-menu-badge", hidden: "" });
    this._btn = scEl("button", { class: `btn btn-ghost ${this.getAttribute("size") === "icon" ? "btn-icon" : "btn-icon-sm"} sc-menu-btn`, type: "button", "aria-label": label, "aria-haspopup": "dialog",
      "aria-expanded": "false", title: hotkey ? `${label} (${hotkey})` : label, onclick: () => this.toggle() },
    icon(this.getAttribute("icon"), "sc-svg"), this._badge);
    this.append(this._btn, this._panel);
    this._outside = (e) => { if (this.open && !this.contains(e.target)) this.hide(); };
    document.addEventListener("pointerdown", this._outside);
    this.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.open) { e.preventDefault(); e.stopPropagation(); this.hide(); this._btn.focus(); }
    });
  }
  disconnectedCallback() { document.removeEventListener("pointerdown", this._outside); }

  get open() { return !!this._panel && !this._panel.hidden; }
  get panel() { return this._panel; }
  set count(n) { this._badge.hidden = !n; this._badge.textContent = n ? String(n) : ""; }

  show() {
    for (const m of document.querySelectorAll("sc-menu")) if (m !== this && m.open) m.hide();
    this._panel.hidden = false;
    this._btn.setAttribute("aria-expanded", "true");
    this._btn.classList.add("on");
    this.dispatchEvent(new CustomEvent("sc-open"));
    const first = this._panel.querySelector("[autofocus], input, select, button, [tabindex='0']");
    if (first) first.focus();
  }
  hide() {
    if (!this.open) return;
    this._panel.hidden = true;
    this._btn.setAttribute("aria-expanded", "false");
    this._btn.classList.remove("on");
    this.dispatchEvent(new CustomEvent("sc-close"));
  }
  toggle() { if (this.open) this.hide(); else this.show(); }
}

// ---------- <sc-filter-list hotkey="F"> ----------
// `.categories = [{ id, label, hint?, icon, options: [{ value, label, count? }] }]`
// `.selected = { [categoryId]: [value, …] }`
// Fires "sc-filter-change" ({ category, value, checked }) and "sc-filter-clear".
// Hover or → on a category opens its options in a flyout; typing searches every option at once.
class ScFilterList extends HTMLElement {
  constructor() { super(); this._cats = []; this._sel = {}; this._open = null; }

  connectedCallback() {
    if (this._input) return;
    this._input = scEl("input", { class: "sc-filter-q", type: "text", placeholder: "Add filter…",
      "aria-label": "Add filter", autocomplete: "off", spellcheck: "false" });
    this._list = scEl("div", { class: "sc-filter-items", role: "menu" });
    this._fly = scEl("div", { class: "dropdown-menu sc-filter-fly", role: "menu", hidden: "" });
    this._foot = scEl("div", { class: "sc-filter-foot" });
    const hotkey = this.getAttribute("hotkey");
    this.append(scEl("div", { class: "sc-filter-head" }, this._input, hotkey ? scEl("kbd", {}, hotkey) : null),
      this._list, this._foot, this._fly);
    this._input.addEventListener("input", () => { this._open = null; this.render(); });
    this.addEventListener("keydown", (e) => this._keys(e));
    this.render();
  }

  set categories(c) { this._cats = c || []; this.render(); }
  get categories() { return this._cats; }
  set selected(s) { this._sel = s || {}; this.render(); }
  get selected() { return this._sel; }
  reset() { if (this._input) this._input.value = ""; this._open = null; this.render(); }

  _isOn(cat, value) { return (this._sel[cat] || []).includes(value); }

  _option(cat, o, prefix) {
    const on = this._isOn(cat.id, o.value);
    return scEl("button", { class: "dropdown-menu-item sc-filter-opt", type: "button", role: "menuitemcheckbox", "aria-checked": String(on),
      tabindex: "-1", "data-key": `${prefix ? "q" : "f"}:${cat.id}:${o.value}`, onclick: () => this._toggle(cat, o) },
    scEl("span", { class: "sc-check" + (on ? " on" : "") }, on ? icon("check", "sc-svg") : null),
    scEl("span", { class: "sc-filter-label" }, prefix ? scEl("span", { class: "sc-muted" }, `${prefix} › `) : null, o.label),
    o.count != null ? scEl("span", { class: "sc-count" }, Number(o.count).toLocaleString()) : null);
  }

  _toggle(cat, o) {
    const checked = !this._isOn(cat.id, o.value);
    this.dispatchEvent(new CustomEvent("sc-filter-change", { detail: { category: cat.id, value: o.value, checked } }));
  }

  render() {
    if (!this._input) return;
    const focused = this.contains(document.activeElement) && document.activeElement.dataset.key;  // keep focus across re-renders
    const q = this._input.value.trim().toLowerCase();
    if (q) {  // search every option across categories
      const hits = this._cats.flatMap((c) => c.options.filter((o) => o.label.toLowerCase().includes(q))
        .map((o) => this._option(c, o, c.label)));
      this._list.replaceChildren(...(hits.length ? hits : [scEl("div", { class: "sc-empty" }, "No matching filters")]));
      this._fly.hidden = true;
    } else {
      this._list.replaceChildren(...this._cats.map((c) => {
        const n = (this._sel[c.id] || []).length;
        return scEl("button", { class: "dropdown-menu-item sc-filter-cat" + (this._open === c.id ? " open" : ""), type: "button",
          role: "menuitem", "aria-haspopup": "menu", "aria-expanded": String(this._open === c.id), tabindex: "-1",
          "data-cat": c.id, "data-key": `c:${c.id}`, onclick: () => this._openCat(c.id, true), onmouseenter: () => this._openCat(c.id) },
        icon(c.icon || "list-filter", "sc-svg"),
        scEl("span", { class: "sc-filter-label" }, c.label, c.hint ? scEl("small", {}, c.hint) : null),
        n ? scEl("span", { class: "sc-count on" }, String(n)) : null,
        icon("chevron-right", "sc-svg sc-chev"));
      }));
      this._renderFly();
    }
    const any = Object.values(this._sel).some((v) => v && v.length);
    this._foot.replaceChildren(...(any ? [scEl("button", { class: "dropdown-menu-item sc-filter-clear", type: "button", tabindex: "-1",
      onclick: () => this.dispatchEvent(new CustomEvent("sc-filter-clear")) }, icon("x", "sc-svg"), "Clear filters")] : []));
    if (focused) { const f = this.querySelector(`[data-key="${CSS.escape(focused)}"]`); if (f) f.focus(); }
  }

  _openCat(id, focus = false) {
    if (this._open !== id) {  // just mark the row and fill the flyout; rebuilding the list would drop focus
      this._open = id;
      for (const b of this._list.querySelectorAll("[data-cat]")) {
        b.classList.toggle("open", b.dataset.cat === id);
        b.setAttribute("aria-expanded", String(b.dataset.cat === id));
      }
      this._renderFly();
    }
    if (focus) { const f = this._fly.querySelector("button"); if (f) f.focus(); }
  }

  _renderFly() {
    const cat = this._cats.find((c) => c.id === this._open);
    if (!cat) { this._fly.hidden = true; return; }
    const row = this._list.querySelector(`[data-cat="${CSS.escape(cat.id)}"]`);
    this._fly.style.top = `${row ? row.offsetTop : 0}px`;
    this._fly.replaceChildren(...(cat.options.length ? cat.options.map((o) => this._option(cat, o))
      : [scEl("div", { class: "sc-empty" }, "Nothing to filter yet")]));
    this._fly.hidden = false;
    // Keep it on screen: slide it left by however much it would hang off the window's right edge.
    this._fly.style.left = "";
    const over = this._fly.getBoundingClientRect().right - (window.innerWidth - 8);
    if (over > 0) this._fly.style.left = `calc(100% + 6px - ${Math.ceil(over)}px)`;
  }

  _keys(e) {
    const inFly = this._fly.contains(document.activeElement);
    const items = [...(inFly ? this._fly : this._list).querySelectorAll("button")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!items.length) return;
      const next = document.activeElement === this._input ? 0 : (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next].focus();
      if (!inFly && items[next].dataset.cat) this._openCat(items[next].dataset.cat);
    } else if (e.key === "ArrowRight" && document.activeElement.dataset && document.activeElement.dataset.cat) {
      e.preventDefault();
      this._openCat(document.activeElement.dataset.cat, true);
    } else if (e.key === "ArrowLeft" && inFly) {
      e.preventDefault();
      const row = this._list.querySelector(`[data-cat="${CSS.escape(this._open)}"]`);
      if (row) row.focus();
    }
  }
}

customElements.define("sc-menu", ScMenu);
customElements.define("sc-filter-list", ScFilterList);


// ---------- Tooltip (shadcn.css .tooltip) ----------
// Every title="…" in the app shows as a shadcn Tooltip: on hover or keyboard focus the title moves into
// data-tooltip (so the browser's own tooltip never appears) and one shared .tooltip is placed above the element.
// Code that sets .title again later (status text, the account email) just works: it's picked up on the next hover.
const scTooltip = (() => {
  const tip = scEl("div", { class: "tooltip", role: "tooltip", hidden: "" });
  let owner = null;
  const GAP = 4 + 5;  // shadcn's sideOffset (4px) plus the arrow
  function textOf(el) {
    if (el.hasAttribute("title")) {
      const t = el.getAttribute("title");
      el.removeAttribute("title");
      if (t) el.dataset.tooltip = t; else delete el.dataset.tooltip;
      if (t && !el.hasAttribute("aria-label") && !el.textContent.trim()) el.setAttribute("aria-label", t);
    }
    return el.dataset.tooltip || "";
  }
  function show(el) {
    const text = textOf(el);
    if (!text) return hide();
    if (!tip.isConnected) document.body.append(tip);
    owner = el;
    tip.textContent = text;
    tip.hidden = false;
    const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
    const side = r.top - t.height - GAP >= 4 ? "top" : "bottom";
    const left = Math.min(Math.max(4, r.left + r.width / 2 - t.width / 2), innerWidth - t.width - 4);
    tip.dataset.side = side;
    tip.style.left = `${left}px`;
    tip.style.top = `${side === "top" ? r.top - t.height - GAP : r.bottom + GAP}px`;
    tip.style.setProperty("--tooltip-arrow-x", `${r.left + r.width / 2 - left}px`);
  }
  function hide() { owner = null; tip.hidden = true; }
  const target = (e) => e.target instanceof Element ? e.target.closest("[title], [data-tooltip]") : null;
  document.addEventListener("mouseover", (e) => { const el = target(e); if (el !== owner) el ? show(el) : hide(); });
  document.addEventListener("focusin", (e) => { const el = target(e); if (el && el.matches(":focus-visible")) show(el); });
  document.addEventListener("focusout", hide);
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("scroll", hide, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && owner) hide(); });
  window.addEventListener("blur", hide);
  return { show, hide };
})();
