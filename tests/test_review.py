"""AI access review: checks, the opt-in config scan (fixture homes only) and the saved report."""
import json

import pytest

from safecontext import review
from safecontext.app.backend import Backend
from safecontext.demo.fake_ai_configs import demo_ai_home

NOW = 1_800_000_000_000
DAY = review.DAY_MS


def ids(r):
    return {c["id"]: c for g in r["groups"] for c in g["checks"]}


def mcp(**kw):
    return {"enabled": False, "grants": 0, "last_used_ms": None, "link_only": False, "keep_awake": False, **kw}


GMAIL = {"id": "gmail", "available": True, "account": "a@example.org", "mode": "full"}


def test_link_only_is_a_risk_and_drives_the_verdict():
    r = review.build_review([GMAIL], mcp(enabled=True, link_only=True, grants=2, last_used_ms=NOW - DAY),
                            {}, None, NOW)
    c = ids(r)
    assert c["sc_link_only"]["status"] == "risk"
    assert c["sc_link"]["status"] == "info" and "2 sign-ins" in c["sc_link"]["detail"]
    assert c["sc_gmail"]["status"] == "ok"
    assert r["tone"] == "risk" and r["verdict"] == "1 thing to fix"


def test_idle_link_is_worth_a_look_and_off_is_ok():
    c = ids(review.build_review([], mcp(enabled=True, last_used_ms=NOW - 40 * DAY), {}, None, NOW))
    assert c["sc_link"]["status"] == "warn"
    assert ids(review.build_review([], mcp(), {}, None, NOW))["sc_link"]["status"] == "ok"
    assert "sc_gmail" not in ids(review.build_review([], mcp(), {}, None, NOW))


def test_guided_answers_and_expiry():
    answers = {"google_connections": {"answer": "ok", "ts": NOW - DAY},
               "grok_connectors": {"answer": "issue", "ts": NOW - DAY},
               "github_apps": {"answer": "skip", "ts": NOW - DAY},
               "mac_screen": {"answer": "ok", "ts": NOW - 91 * DAY}}
    c = ids(review.build_review([], mcp(), answers, None, NOW))
    assert c["google_connections"]["status"] == "ok"
    assert c["grok_connectors"]["status"] == "risk"
    assert c["github_apps"]["status"] == "info"
    assert c["mac_screen"]["status"] == "todo" and c["mac_screen"]["answer"] == ""  # expired
    assert c["claude_connectors"]["status"] == "todo"


def test_load_answers_ignores_junk():
    assert review.load_answers("not json") == {}
    assert review.load_answers(json.dumps({"nope": {"answer": "ok"}, "mac_screen": {"answer": "maybe"},
                                           "mac_accessibility": {"answer": "ok", "ts": 1}})) == {
        "mac_accessibility": {"answer": "ok", "ts": 1}}


def test_guided_links_are_official_pages_or_settings_panes():
    for cid, *_rest, link in review.GUIDED:
        assert link == "" or link.startswith("https://") or link.startswith("x-apple.systempreferences:")


@pytest.mark.parametrize("rule,level", [
    ("Bash", "risk"), ("Bash(*)", "risk"), ("Bash(:*)", "risk"), ("WebFetch", "warn"), ("Write", "warn"),
    ("Bash(curl:*)", "warn"), ("Bash(sudo rm -rf:*)", "warn"), ("Bash(npm test:*)", None),
    ("WebFetch(domain:docs.python.org)", None), ("Read(~/projects/**)", None), ("mcp__x__y", None)])
def test_broad_rules(rule, level):
    assert review.broad_rule(rule) == level


def test_scan_reads_names_never_values(tmp_path):
    home = demo_ai_home(tmp_path)
    scan = review.scan_ai_configs(home)
    blob = json.dumps(scan)
    assert "demo-not-a-real-key" not in blob and "/Users/alex" not in blob and "example.invalid" not in blob
    d = {s["name"]: s for s in scan["claude_desktop"]["servers"]}
    assert d["weather"]["secrets"] and d["safecontext"]["own"] and d["filesystem"]["command"] == "npx"
    c = ids(review.build_review([], mcp(), {}, scan, NOW))
    assert c["lc_desktop"]["status"] == "warn" and len(c["lc_desktop"]["items"]) == 2
    assert c["lc_desktop_secrets"]["items"] == ["weather"]
    assert c["lc_code_mode"]["status"] == "ok"
    assert c["lc_code_allow"]["status"] == "warn" and set(c["lc_code_allow"]["items"]) == {"Bash(curl:*)", "WebFetch"}
    assert set(c["lc_code_mcp"]["items"]) == {"github", "postgres (one project)"}


def test_scan_flags_bypass_and_handles_missing_or_broken_files(tmp_path):
    assert review.scan_ai_configs(tmp_path) == {"claude_desktop": None, "claude_code": None}
    c = ids(review.build_review([], mcp(), {}, review.scan_ai_configs(tmp_path), NOW))
    assert c["lc_desktop"]["status"] == "ok" and c["lc_code"]["status"] == "ok"
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash"]}}))
    d = tmp_path / "Library" / "Application Support" / "Claude"
    d.mkdir(parents=True)
    (d / "claude_desktop_config.json").write_text("{broken")
    c = ids(review.build_review([], mcp(), {}, review.scan_ai_configs(tmp_path), NOW))
    assert c["lc_code_mode"]["status"] == "risk" and c["lc_code_allow"]["status"] == "risk"
    assert c["lc_desktop"]["status"] == "ok"  # unreadable config treated as none


def test_report_is_self_contained_and_leaves_out_private_values(tmp_path):
    scan = review.scan_ai_configs(demo_ai_home(tmp_path))
    r = review.build_review([GMAIL], mcp(enabled=True, link_only=True, last_used_ms=NOW), {}, scan, NOW)
    page = review.report_html(r, "0.1.5")
    assert "<script" not in page and "http-equiv='Content-Security-Policy'" in page
    assert "a@example.org" not in page and "demo-not-a-real-key" not in page
    assert "Link-only mode is on" in page and "https://myaccount.google.com/connections" in page
    assert "x-apple" not in page


@pytest.fixture
def backend(tmp_path):
    b = Backend(tmp_path / "data", demo=True, auto_sync=False, review_home=demo_ai_home(tmp_path / "home"))
    yield b
    b._shutdown()


def test_backend_review_flow(backend, tmp_path):
    r = backend.review()
    assert r["ok"] and r["scan_on"] is False and "lc_desktop" not in ids(r)
    r = backend.review_set_scan(True)
    assert r["scan_on"] and ids(r)["lc_desktop"]["status"] == "warn"
    r = backend.review_answer("google_connections", "ok")
    assert ids(r)["google_connections"]["status"] == "ok"
    assert ids(backend.review_answer("google_connections", ""))["google_connections"]["status"] == "todo"
    assert backend.review_answer("rm -rf", "ok")["ok"] is False
    assert backend.review_answer("google_connections", "yes please")["ok"] is False
    assert backend.review_open("google_connections") == {"ok": True, "opened": False}  # demo opens nothing
    assert backend.review_open("sc_link")["ok"] is False
    saved = backend.review_save_report()
    assert saved["ok"] and saved["opened"] is False
    assert saved["path"].startswith(str(tmp_path / "data" / "reports"))


def test_demo_backend_without_home_never_scans_the_real_home(tmp_path):
    b = Backend(tmp_path / "data", demo=True, auto_sync=False)
    try:
        assert b._review_home == tmp_path / "data" / "demo-home"
    finally:
        b._shutdown()
