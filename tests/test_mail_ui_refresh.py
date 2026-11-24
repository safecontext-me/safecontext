"""The browse page's list must pick up mail that synced while another screen was up.

Runs mail.js in Node's vm with the DOM and the backend stubbed out (skipped without Node).
Regression: 0.1.3 kept showing the list it loaded first; a background sync that finished
while the user was on another screen never reached it, so new mail looked "not synced".
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

MAIL_JS = Path(__file__).resolve().parents[1] / "src" / "safecontext" / "app" / "ui" / "mail.js"
NODE = shutil.which("node") or (str(Path.home() / ".local/bin/node")
                                if os.access(Path.home() / ".local/bin/node", os.X_OK) else None)

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file, scenario] = process.argv.slice(2);
const store = { rows: ["old-1", "old-2"], kept: 2 };
const calls = { list: 0 };
const list = { scrollTop: 0, addEventListener() {} };
const ctx = {
  console, Promise, Date, Number, Object, JSON,
  setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout,
  document: { addEventListener() {} }, window: { addEventListener() {} },
  state: { view: "mail", status: { sources: [] } },
  $: (id) => id === "mail-list" ? list : { value: "", hidden: false, addEventListener() {}, replaceChildren() {} },
  api: () => ({
    mail_list: async () => { calls.list++; return { ok: true, rows: store.rows.map((s) => ({ type: "item", source: "gmail", thread_id: s, subject: s })), next: null }; },
    mail_folders: async () => ({ ok: true, mailboxes: {}, sources: [] }),
    mail_coverage: async () => ({ ok: true, sources: [{ id: "gmail", kept: store.kept, dropped: 0, newest_ms: store.kept, syncing: false }] }),
  }),
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
// Rendering isn't under test.
vm.runInContext(`mailRender = mailRenderFolders = mailRenderCoverage = mailPlaceholder = mailLoadReasons = () => {};
                 mail.inited = true; mail.source = "gmail";`, ctx);
const tick = () => new Promise((r) => setTimeout(r, 20));
(async () => {
  vm.runInContext("mailShow()", ctx); await tick();            // open Gmail's page
  vm.runInContext("state.view = 'mcp'", ctx);                   // go to another screen
  store.rows = ["new-1", "old-1", "old-2"]; store.kept = 3;     // a background sync stores new mail
  if (scenario === "nothing_new") { store.rows = ["old-1", "old-2"]; store.kept = 2; }
  const before = calls.list;
  vm.runInContext("state.view = 'mail'; mailShow()", ctx); await tick();  // come back
  console.log(JSON.stringify({ top: vm.runInContext("mail.rows[0].subject", ctx), reloads: calls.list - before }));
})();
"""


def _run(tmp_path, scenario):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run([NODE, str(h), str(MAIL_JS), scenario], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_coming_back_to_the_page_shows_mail_synced_meanwhile(tmp_path):
    r = _run(tmp_path, "new_mail")
    assert r["top"] == "new-1"
    assert r["reloads"] == 1


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_coming_back_without_changes_keeps_the_list(tmp_path):
    r = _run(tmp_path, "nothing_new")
    assert r["top"] == "old-1"
    assert r["reloads"] == 0
