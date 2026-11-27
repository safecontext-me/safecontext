"""Label and tag chips on mail rows (mail.js rowLabels / labelDot), run in Node's vm with a tiny `el`
stub (skipped without Node): Gmail's own labels stay off rows (Starred becomes a star), at most two
chips then "+n", and colours that aren't a known tag colour or a #rrggbb hex fall back to gray."""
import json
import subprocess

import pytest

from tests.test_mail_row_time import NODE, UI

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = (tag, attrs = {}, ...kids) => ({ tag, cls: attrs.class || "", style: attrs.style, title: attrs.title,
  kids: kids.flat().filter((k) => k != null && k !== false) });
const ctx = { console, Date, Number, Object, JSON, Set, el, icon: (n) => ({ tag: "svg", cls: "icon-" + n, kids: [] }),
  document: { addEventListener() {} }, window: { addEventListener() {} }, state: {},
  $: () => ({ value: "", addEventListener() {} }), richText: (s) => s, n: String };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const rowLabels = vm.runInContext("rowLabels", ctx), labelDot = vm.runInContext("labelDot", ctx);
const L = (name, type, color = "") => ({ id: name, name, type, color });
const flat = (node) => node == null ? null : typeof node === "string" ? node
  : { cls: node.cls, title: node.title ?? null, kids: node.kids.map(flat) };
console.log(JSON.stringify({
  none: flat(rowLabels({ labels: [L("Inbox", "system"), L("Sent", "system")] })),
  many: flat(rowLabels({ labels: [L("Inbox", "system"), L("Starred", "system"), L("A", "user", "#16a766"),
    L("B", "user"), L("C", "tag", "green"), L("D", "tag", "blue")] })),
  dots: [labelDot(L("x", "user", "#16a766")).style, labelDot(L("x", "user", "red;background:url(x)")).style,
         labelDot(L("x", "tag", "green")).style, labelDot(L("x", "tag", "var(--x)")).style],
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_row_label_chips(tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run([NODE, str(h), str(UI / "mail.js")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["none"] is None  # only Gmail's own labels: nothing on the row
    kids = r["many"]["kids"]
    assert kids[0]["cls"] == "lbl-star"
    assert [k["kids"][1]["kids"][0] for k in kids[1:3]] == ["A", "B"]
    assert kids[3] == {"cls": "lbl-more", "title": "C, D", "kids": ["+2"]}
    assert r["dots"] == ["background:#16a766", "background:var(--tag-gray)",
                         "background:var(--tag-green)", "background:var(--tag-gray)"]
