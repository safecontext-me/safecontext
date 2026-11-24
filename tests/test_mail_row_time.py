"""The mail list shows each message's date, and its time while the row is hovered.

mailRow() runs in Node's vm with a tiny `el` stub (skipped without Node); the CSS rules
are checked as text, since the swap itself is pure CSS.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "src" / "safecontext" / "app" / "ui"
NODE = shutil.which("node") or (str(Path.home() / ".local/bin/node")
                                if os.access(Path.home() / ".local/bin/node", os.X_OK) else None)

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file] = process.argv.slice(2);
const el = (tag, attrs = {}, ...kids) => ({ tag, cls: attrs.class || "",
  kids: kids.flat().filter((k) => k != null && k !== false) });
const ctx = {
  console, Date, Number, Object, JSON, el, document: { addEventListener() {} }, window: { addEventListener() {} },
  state: {}, $: () => ({ value: "", addEventListener() {} }),
  richText: (s) => s, senderName: (s) => s, sourceName: (s) => s, n: String,
  fmtDate: (iso, withTime) => (withTime ? "DATE+TIME" : "DATE"),
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
vm.runInContext("mail.folders = null; mail.sel = -1;", ctx);
const iso = "2026-03-04T15:42:00Z";
const expectTime = new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const find = (node, cls) => {
  if (!node || typeof node !== "object") return null;
  if (node.cls.split(" ").includes(cls)) return node;
  for (const k of node.kids) { const f = find(k, cls); if (f) return f; }
  return null;
};
const show = (r) => {
  const d = find(vm.runInContext("mailRow", ctx)(r, 0), "mrow-date");
  return { cls: d.cls, kids: d.kids.map((k) => typeof k === "object" ? [k.cls, k.kids.join("")] : k) };
};
console.log(JSON.stringify({
  expectTime,
  item: show({ type: "item", kind: "mail", sender: "a", date: iso, title: "t" }),
  dropped: show({ type: "dropped", date: iso, reason_info: { label: "l", why: "w" } }),
  event: show({ type: "item", kind: "event", source: "cal", date: iso, title: "t" }),
  nodate: show({ type: "dropped", date: null, reason_info: { label: "l", why: "w" } }),
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_rows_render_date_and_time_for_the_hover_swap(tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run([NODE, str(h), str(UI / "mail.js")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    both = {"cls": "mrow-date when", "kids": [["when-d", "DATE"], ["when-t", r["expectTime"]]]}
    assert r["item"] == both          # mail: date, plus the Mac-locale time
    assert r["dropped"] == both       # not-synced rows too
    assert r["event"] == {"cls": "mrow-date", "kids": ["DATE+TIME"]}  # events already show their time
    assert r["nodate"] == {"cls": "mrow-date", "kids": [""]}


def test_css_shows_time_only_on_hover_without_resizing():
    css = re.sub(r"\s+", " ", (UI / "mail.css").read_text())
    assert ".mrow-date.when { display: inline-grid;" in css
    assert ".mrow-date.when > span { grid-area: 1 / 1; }" in css   # stacked: width = the wider of the two
    assert ".mrow-date.when .when-t, .mrow:hover .mrow-date.when .when-d { visibility: hidden; }" in css
    assert ".mrow:hover .mrow-date.when .when-t { visibility: visible; }" in css
