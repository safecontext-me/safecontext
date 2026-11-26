// Icons: the one place the app's icons come from.
//
// Lucide icons (lucide.dev, ISC license; the icon set shadcn uses) are vendored unchanged as files in
// ui/vendor/lucide/. main.py's build_html reads them and puts their shapes in LUCIDE (name -> [[tag, attrs], …]),
// so no file is fetched at run time. To add an icon, drop its .svg from lucide-icons/lucide into that folder.
//
//   icon("search", "nav-svg")                    an <svg> element, for JS-built UI
//   <svg data-lucide="search" class="nav-svg">   in index.html: filled in when the page loads
//   <sc-icon name="search">                      a custom element that holds one icon
//   <svg data-mark="mcp">                        a non-Lucide mark, copied from <template id="icon-mcp">
//
// Non-Lucide marks (the MCP mark, Gmail and other source logos) live in <template id="icon-…"> in index.html.
// Shapes are built with createElementNS from data, never as HTML.
"use strict";

const LUCIDE = window.LUCIDE || {};  // set by build_html (see main.py)
const SVG_NS = "http://www.w3.org/2000/svg";

// Lucide's own defaults: 24px grid, stroked in the text color.
function fillIcon(svg, name) {
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.replaceChildren();
  for (const [tag, attrs] of LUCIDE[name] || []) {
    const s = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
    svg.append(s);
  }
  if (!LUCIDE[name]) console.warn(`icon "${name}" is not in ui/vendor/lucide`);
  return svg;
}

function icon(name, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  if (cls) svg.setAttribute("class", cls);
  return fillIcon(svg, name);
}

class ScIcon extends HTMLElement {
  connectedCallback() { this.replaceChildren(icon(this.getAttribute("name"))); }
}
customElements.define("sc-icon", ScIcon);

// Placeholders in index.html (scripts run after the markup, so they're all there):
// <svg data-lucide="…"> gets a Lucide icon; <svg data-mark="…"> gets a copy of <template id="icon-…">.
for (const svg of document.querySelectorAll("svg[data-lucide]")) fillIcon(svg, svg.dataset.lucide);
for (const svg of document.querySelectorAll("svg[data-mark]")) {
  const src = document.getElementById(`icon-${svg.dataset.mark}`)?.content.querySelector("svg");
  if (!src) { console.warn(`no <template id="icon-${svg.dataset.mark}">`); continue; }
  for (const a of src.attributes) if (a.name !== "class" && a.name !== "xmlns") svg.setAttribute(a.name, a.value);
  svg.replaceChildren(...[...src.childNodes].map((n) => n.cloneNode(true)));
}
