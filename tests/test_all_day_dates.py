"""All-day events are stored at UTC midnight. The list row, its tooltip, the search dropdown and the
event view must all read them in UTC, or anyone west of Greenwich sees the day before. Timed items
stay in local time.

Runs app.js's date helpers and mail.js in Node's vm (skipped without Node) with TZ=America/Los_Angeles
and the en-US locale pinned.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "src" / "safecontext" / "app" / "ui"
NODE = shutil.which("node") or (str(Path.home() / ".local/bin/node")
                                if os.access(Path.home() / ".local/bin/node", os.X_OK) else None)

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [helpers, mailJs] = process.argv.slice(2);
for (const name of ["toLocaleString", "toLocaleDateString", "toLocaleTimeString"]) {
  const f = Date.prototype[name];
  Date.prototype[name] = function (_l, o) { return f.call(this, "en-US", o); };
}
const el = (tag, attrs = {}, ...kids) => ({ tag, cls: attrs.class || "", title: attrs.title,
  kids: kids.flat().filter((k) => k != null && k !== false) });
let dropdown = null;
const ctx = {
  console, Date, Number, Object, JSON, el, document: { addEventListener() {} }, window: { addEventListener() {} },
  state: {}, $: () => ({ value: "", addEventListener() {}, replaceChildren(...k) { dropdown = k; },
    querySelector: () => null, classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {}, hidden: false }),
  richText: (s) => s, senderName: (s) => s, sourceName: (s) => s, n: String, sourceIcon: () => null,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(helpers, "utf8"), ctx);
vm.runInContext(fs.readFileSync(mailJs, "utf8"), ctx);
vm.runInContext("mail.folders = null; mail.sel = -1;", ctx);
const find = (node, cls) => {
  if (!node || typeof node !== "object") return null;
  if (node.cls.split(" ").includes(cls)) return node;
  for (const k of node.kids) { const f = find(k, cls); if (f) return f; }
  return null;
};
const row = (r) => { const d = find(vm.runInContext("mailRow", ctx)(r, 0), "mrow-date");
  return { text: d.kids.join(""), title: d.title }; };
const searchMeta = (r) => {
  ctx.__r = r;
  vm.runInContext("search.text = 'x'; search.active = -1; search.results = [__r]; searchRender(null)", ctx);
  return find(dropdown[0], "sr-meta").kids.join("");
};
const y = new Date().getFullYear();
const oneDay = { type: "thread", kind: "event", source: "gcal", title: "Holiday", all_day: true,
  date: `${y}-03-04T00:00:00.000Z`, end: `${y}-03-05T00:00:00.000Z` };
const threeDays = { ...oneDay, end: `${y}-03-07T00:00:00.000Z` };
const timed = { type: "thread", kind: "event", source: "gcal", title: "Standup", all_day: false,
  date: `${y}-03-04T01:30:00.000Z`, end: `${y}-03-04T02:00:00.000Z` };
const mailRowLate = { type: "thread", kind: "message", sender: "a", title: "t", date: `${y}-03-04T01:30:00.000Z` };
console.log(JSON.stringify({
  year: y,
  oneDay: row(oneDay), threeDays: row(threeDays), timed: row(timed), mail: row(mailRowLate),
  searchAllDay: searchMeta(oneDay), searchTimed: searchMeta(timed),
  range: vm.runInContext("fmtRange", ctx)(threeDays.date, threeDays.end, true),
}));
"""


def _functions(src: str, names) -> str:
    """The top-level `function name(...) { ... }` blocks of app.js, which as a whole needs the page."""
    out = []
    for name in names:
        m = re.search(rf"^function {name}\(.*?^\}}\n", src, re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "\n".join(out)


@pytest.fixture
def shown(tmp_path):
    if NODE is None:
        pytest.skip("needs Node")
    app = (UI / "app.js").read_text()
    helpers = tmp_path / "helpers.js"
    helpers.write_text(_functions(app, ["fmtDate", "fmtAllDay", "allDayLast", "fmtRange"]))
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run([NODE, str(h), str(helpers), str(UI / "mail.js")], capture_output=True, text=True,
                         timeout=30, env={**os.environ, "TZ": "America/Los_Angeles"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _long(y, m, d):
    return date(y, m, d).strftime("%a, %b %-d, %Y")


def test_all_day_rows_show_their_own_day_west_of_utc(shown):
    y = shown["year"]
    assert shown["oneDay"] == {"text": "Mar 4", "title": _long(y, 3, 4)}   # not Mar 3, and no time
    assert shown["threeDays"] == {"text": "Mar 4", "title": f"{_long(y, 3, 4)} – {_long(y, 3, 6)}"}
    assert shown["range"] == "Mar 4 – Mar 6 · all day"                     # event view agrees


def test_timed_items_stay_in_local_time(shown):
    # 01:30 UTC on Mar 4 is 5:30 PM on Mar 3 in Los Angeles.
    assert shown["timed"]["text"] == "Mar 3, 5:30 PM"
    assert shown["timed"]["title"].endswith("Mar 3, %d, 5:30 PM" % shown["year"])
    assert shown["mail"]["text"] == "Mar 3"


def test_search_dropdown_uses_the_same_day(shown):
    assert shown["searchAllDay"] == "gcal · Mar 4"
    assert shown["searchTimed"] == "gcal · Mar 3"
