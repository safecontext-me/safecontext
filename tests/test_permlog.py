"""Permission log (permlog.py): local only, bounded, plain sentences, scan diffs."""
import json

from safecontext import permlog
from safecontext.permlog import PermissionLog

DAY = permlog.DAY_MS


def test_record_trims_cleans_and_survives_a_restart(tmp_path):
    now = [1_800_000_000_000]
    path = tmp_path / "permission_log.json"
    log = PermissionLog(path, now=lambda: now[0])
    assert log.record("nonsense") is None and log.record("connect", "maybe") is None
    log.record("connect", "approved", client="Grok\n" + "x" * 100)
    assert log.entries()[0]["client"] == ("Grok " + "x" * 100)[:60]
    now[0] += 400 * DAY
    log.record("link", "on")
    assert [e["kind"] for e in log.entries()] == ["link"]  # older than a year is dropped
    assert [e["kind"] for e in PermissionLog(path).entries()] == ["link"]
    for _ in range(permlog.KEEP + 5):
        log.record("keep_awake", "on")
    assert len(json.loads(path.read_text())) == permlog.KEEP
    log.clear()
    assert PermissionLog(path).entries() == []


def test_bad_file_is_ignored(tmp_path):
    (tmp_path / "p.json").write_text("{nope")
    assert PermissionLog(tmp_path / "p.json").entries() == []
    (tmp_path / "q.json").write_text(json.dumps([{"kind": "connect", "ts_ms": 1}, {"kind": "evil", "ts_ms": 2}, 3]))
    assert [e["kind"] for e in PermissionLog(tmp_path / "q.json").entries()] == ["connect"]


def test_describe_is_plain():
    d = permlog.describe
    assert d({"kind": "connect", "outcome": "denied", "client": "Grok"}) == "Grok asked to connect. You denied it"
    assert d({"kind": "connect", "outcome": "expired"}).startswith("An AI app asked to connect. No answer")
    assert d({"kind": "link", "outcome": "regenerated"}).startswith("New MCP link made")
    assert d({"kind": "signins", "note": "1 → 2"}) == "Sign-ins held by AI apps: 1 → 2"


def test_scan_facts_and_changes():
    before = permlog.scan_facts({"claude_desktop": {"servers": [{"name": "notes", "own": False},
                                                                {"name": "safecontext", "own": True}]},
                                 "claude_code": None})
    after = permlog.scan_facts({"claude_desktop": {"servers": []},
                                "claude_code": {"servers": [{"name": "github", "own": False}], "project_servers": [],
                                                "broad": [{"rule": "Bash(*)"}], "mode": "bypassPermissions"}})
    assert permlog.scan_changes(before, after) == [
        "Claude Code: MCP server added: github",
        "Claude Code: broad allow rule added: Bash(*)",
        "Claude Code: skips permission prompts (added)",
        "Claude Desktop: MCP server removed: notes"]
    assert permlog.scan_facts(None) == set()
