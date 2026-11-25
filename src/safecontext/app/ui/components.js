// Small UI components as native Web Components (custom elements). No library and no build step.
// They render into the light DOM, so app.css / components.css theme them like the rest of the app.
// As everywhere in the app, text only ever goes in as text, never as HTML.
//
//   <sc-icon name="check">                    a Lucide icon (lucide.dev, ISC license)
//   <sc-menu icon="…" label="…" hotkey="F">   an icon button that opens a popover with its children
//   <sc-filter-list>                          "Add filter…" search, categories, and a flyout of options
"use strict";

// Lucide icons used by the components: [tag, attributes] per shape, on a 24px grid.
const SC_ICONS = {
  "list-filter": [["path", { d: "M2 5h20" }], ["path", { d: "M6 12h12" }], ["path", { d: "M9 19h6" }]],
  "sliders-horizontal": ["M10 5H3", "M12 19H3", "M14 3v4", "M16 17v4", "M21 12h-9", "M21 19h-5", "M21 5h-7", "M8 10v4", "M8 12H3"]
    .map((d) => ["path", { d }]),
  "chevron-right": [["path", { d: "m9 18 6-6-6-6" }]],
  check: [["path", { d: "M20 6 9 17l-5-5" }]],
  ban: [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "M4.929 4.929 19.07 19.071" }]],
  x: [["path", { d: "M18 6 6 18" }], ["path", { d: "m6 6 12 12" }]],
  ellipsis: [["circle", { cx: "12", cy: "12", r: "1" }], ["circle", { cx: "19", cy: "12", r: "1" }], ["circle", { cx: "5", cy: "12", r: "1" }]],
  // AI access review: a distinct shape per colour, so colour is never the only cue
  "circle-x": [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "m15 9-6 6" }], ["path", { d: "m9 9 6 6" }]],
  "triangle-alert": [["path", { d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" }],
    ["path", { d: "M12 9v4" }], ["path", { d: "M12 17h.01" }]],
  "circle-check": [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "m9 12 2 2 4-4" }]],
  "circle-help": [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" }],
    ["path", { d: "M12 17h.01" }]],
  "external-link": [["path", { d: "M15 3h6v6" }], ["path", { d: "M10 14 21 3" }],
    ["path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }]],
};

function scIcon(name, cls = "") {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `sc-svg ${cls}`.trim());
  for (const [tag, attrs] of SC_ICONS[name] || []) {
    const s = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
    svg.append(s);
  }
  return svg;
}

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

// ---------- <sc-icon name="…"> ----------
class ScIcon extends HTMLElement {
  connectedCallback() { this.replaceChildren(scIcon(this.getAttribute("name"))); }
}

// ---------- <sc-menu icon="…" label="…" hotkey="F"> ----------
// align="end" opens the panel from the right edge; compact makes it fit a short list of .sc-item actions.
// An icon button that toggles a popover holding the element's original children.
// Closes on outside click, Esc, or another menu opening. Fires "sc-open" / "sc-close".
// `.count = n` shows a small badge on the button (e.g. how many filters are on).
class ScMenu extends HTMLElement {
  connectedCallback() {
    if (this._panel) return;
    const label = this.getAttribute("label") || "";
    const hotkey = this.getAttribute("hotkey");
    this._panel = scEl("div", { class: "sc-menu-panel", role: "dialog", "aria-label": label, hidden: "" },
      ...this.childNodes);
    this._badge = scEl("span", { class: "sc-menu-badge", hidden: "" });
    this._btn = scEl("button", { class: "sc-menu-btn", type: "button", "aria-label": label, "aria-haspopup": "dialog",
      "aria-expanded": "false", title: hotkey ? `${label} (${hotkey})` : label, onclick: () => this.toggle() },
    scIcon(this.getAttribute("icon")), this._badge);
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
    this._fly = scEl("div", { class: "sc-filter-fly", role: "menu", hidden: "" });
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
    return scEl("button", { class: "sc-filter-opt", type: "button", role: "menuitemcheckbox", "aria-checked": String(on),
      tabindex: "-1", "data-key": `${prefix ? "q" : "f"}:${cat.id}:${o.value}`, onclick: () => this._toggle(cat, o) },
    scEl("span", { class: "sc-check" + (on ? " on" : "") }, on ? scIcon("check") : null),
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
        return scEl("button", { class: "sc-filter-cat" + (this._open === c.id ? " open" : ""), type: "button",
          role: "menuitem", "aria-haspopup": "menu", "aria-expanded": String(this._open === c.id), tabindex: "-1",
          "data-cat": c.id, "data-key": `c:${c.id}`, onclick: () => this._openCat(c.id, true), onmouseenter: () => this._openCat(c.id) },
        scIcon(c.icon || "list-filter"),
        scEl("span", { class: "sc-filter-label" }, c.label, c.hint ? scEl("small", {}, c.hint) : null),
        n ? scEl("span", { class: "sc-count on" }, String(n)) : null,
        scIcon("chevron-right", "sc-chev"));
      }));
      this._renderFly();
    }
    const any = Object.values(this._sel).some((v) => v && v.length);
    this._foot.replaceChildren(...(any ? [scEl("button", { class: "sc-filter-clear", type: "button", tabindex: "-1",
      onclick: () => this.dispatchEvent(new CustomEvent("sc-filter-clear")) }, scIcon("x"), "Clear filters")] : []));
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

customElements.define("sc-icon", ScIcon);
customElements.define("sc-menu", ScMenu);
customElements.define("sc-filter-list", ScFilterList);
