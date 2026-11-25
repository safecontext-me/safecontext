"""The mail list's date column shows the date; the full date and time is a hover tooltip.

The tooltip is the native `title` (WebKit shows it after its own hover delay, as Gmail does), in
Gmail's format: "Wed, Mar 4, 2026, 3:42 PM". mailRow() runs in Node's vm with a tiny `el` stub
(skipped without Node), in a fixed time zone and locale.
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
const [file, locale] = process.argv.slice(2);
// Pin the locale the page sees (the Mac's, in the app) so the expected text is fixed.
const toLocaleString = Date.prototype.toLocaleString;
Date.prototype.toLocaleString = function (_l, o) { return toLocaleString.call(this, locale, o); };
const el = (tag, attrs = {}, ...kids) => ({ tag, cls: attrs.class || "", title: attrs.title,
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
const find = (node, cls) => {
  if (!node || typeof node !== "object") return null;
  if (node.cls.split(" ").includes(cls)) return node;
  for (const k of node.kids) { const f = find(k, cls); if (f) return f; }
  return null;
};
const show = (r) => {
  const d = find(vm.runInContext("mailRow", ctx)(r, 0), "mrow-date");
  return { cls: d.cls, title: d.title ?? null, kids: d.kids };
};
console.log(JSON.stringify({
  item: show({ type: "item", kind: "mail", sender: "a", date: iso, title: "t" }),
  dropped: show({ type: "dropped", date: iso, reason_info: { label: "l", why: "w" } }),
  event: show({ type: "item", kind: "event", source: "cal", date: iso, title: "t" }),
  nodate: show({ type: "dropped", date: null, reason_info: { label: "l", why: "w" } }),
}));
"""


def _rows(tmp_path, locale):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run([NODE, str(h), str(UI / "mail.js"), locale], capture_output=True, text=True,
                         timeout=30, env={**os.environ, "TZ": "UTC"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_rows_show_the_date_with_a_gmail_style_tooltip(tmp_path):
    r = _rows(tmp_path, "en-US")
    tip = "Wed, Mar 4, 2026, 3:42 PM"   # Gmail's hover format
    assert r["item"] == {"cls": "mrow-date", "title": tip, "kids": ["DATE"]}      # date only, no time
    assert r["dropped"] == {"cls": "mrow-date", "title": tip, "kids": ["DATE"]}   # not-synced rows too
    assert r["event"] == {"cls": "mrow-date", "title": tip, "kids": ["DATE+TIME"]}  # events keep their time
    assert r["nodate"] == {"cls": "mrow-date", "title": None, "kids": [""]}


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_tooltip_follows_the_locale_clock(tmp_path):
    assert _rows(tmp_path, "en-GB")["item"]["title"] == "Wed, 4 Mar 2026, 15:42"


def test_no_hover_swap_left():
    css = (UI / "mail.css").read_text()
    js = (UI / "mail.js").read_text()
    assert not re.search(r"\.when\b|when-[dt]", css)
    assert "when-d" not in js and "when-t" not in js
