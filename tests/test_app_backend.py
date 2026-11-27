"""The object the app window calls. Runs fully offline in demo wiring."""
import re
import time
from pathlib import Path

import pytest

from safecontext import auth
from safecontext.app.backend import Backend
from safecontext.app.demo import DEMO_ACCOUNT, make_fake_gmail
from safecontext.app.main import build_html
from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.synthetic import mailbox


def wait_sync(b, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if not b.status()["syncing"]:
            return b.status()
        time.sleep(0.05)
    raise AssertionError("sync did not finish")


@pytest.fixture
def gmail():
    g = FakeGmail(page_size=5, email=DEMO_ACCOUNT)
    for m in mailbox():
        g.add(m, record=False)
    return g


@pytest.fixture
def backend(tmp_path, gmail):
    b = Backend(tmp_path / "data", demo=True, gmail_factory=lambda a, m: gmail,
                connect_fn=lambda mode, email: DEMO_ACCOUNT, auto_sync=False)
    yield b
    b._shutdown()


def test_starts_disconnected(backend):
    s = backend.status()
    assert s["ok"] and s["account"] is None and s["demo"] is True


def test_connect_then_sync(backend):
    r = backend.connect("full", "")
    assert r == {"ok": True, "account": DEMO_ACCOUNT}
    s = wait_sync(backend)
    assert s["kept"] == 7 and s["first_sync_done"] and not s["needs_reconnect"]
    assert s["last_result"]["status"] == "ok"


def test_bad_mode(backend):
    assert backend.connect("everything")["ok"] is False


def test_search_read_and_links(backend):
    backend.connect("full")
    wait_sync(backend)
    r = backend.search("rent", "", "", "")
    assert r["ok"] and r["results"][0]["subject"] == "Rent split"
    t = backend.read_thread(r["results"][0]["thread_id"])
    assert "[REDACTED:IBAN]" in t["messages"][0]["body"]
    assert backend.recent("@example.com")["count"] == 3


def test_tool_errors_come_back_as_messages(backend):
    r = backend.search("x", "", "not-a-date", "")
    assert r["ok"] is False and "invalid" in r["error"]


def test_only_gmail_links_open(backend, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
    assert backend.open_in_gmail("https://mail.google.com/mail/?authuser=a#all/1")["ok"]
    for bad in ["https://evil.example/phish", "file:///etc/passwd", "javascript:alert(1)",
                "https://mail.google.com.evil.example/mail/"]:
        assert backend.open_in_gmail(bad)["ok"] is False
    assert opened == ["https://mail.google.com/mail/?authuser=a#all/1"]


def test_privacy_report_uses_plain_labels(backend):
    backend.connect("full")
    wait_sync(backend)
    r = backend.privacy_report()
    assert r["kept"] == 7
    assert "Security & 2FA codes" in r["dropped"] and "Card numbers" in r["redacted"]
    assert not any(re.fullmatch(r"[A-Z_]+", k) for k in r["redacted"])


def test_revoked_shows_reconnect(backend, gmail):
    backend.connect("full")
    wait_sync(backend)
    gmail.revoked = True
    backend.sync_now()
    s = wait_sync(backend)
    assert s["needs_reconnect"] and "Reconnect Gmail" in s["reconnect_message"]
    assert backend.search("rent", "", "", "")["ok"]  # local mail still browsable
    gmail.revoked = False
    backend.connect("full")
    assert not wait_sync(backend)["needs_reconnect"]


def test_gmail_factory_auth_failure_is_reconnect(tmp_path):
    def factory(account, mode):
        raise auth.NeedsReconnect("revoked_or_expired")

    b = Backend(tmp_path, demo=True, gmail_factory=factory, connect_fn=lambda m, e: DEMO_ACCOUNT, auto_sync=False)
    b.connect("full")
    assert wait_sync(b)["needs_reconnect"]


def test_switching_accounts_blocked(backend):
    backend.connect("full")
    wait_sync(backend)
    backend._connect_fn = lambda mode, email: "someone@else.example"
    r = backend.connect("full")
    assert r["ok"] is False and "Disconnect" in r["error"]


def test_disconnect_and_wipe(backend):
    backend.connect("full")
    wait_sync(backend)
    assert backend.disconnect(False)["ok"] and backend.status()["needs_reconnect"]
    assert backend.disconnect(True) == {"ok": True, "wiped": True}
    s = backend.status()
    assert s["account"] is None and s["kept"] == 0


def test_unexpected_errors_hide_details(backend, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret 4111 1111 1111 1111")

    monkeypatch.setattr(backend._store, "stats", boom)
    r = backend.privacy_report()
    assert r == {"ok": False, "error": "Something went wrong (RuntimeError)."}


def test_window_cannot_reach_internals(backend):
    """pywebview exposes public attributes to JavaScript; only the intended methods may be public."""
    public = {n for n in dir(backend) if not n.startswith("_")}
    assert public == {"status", "connect", "sync_now", "disconnect", "search", "recent", "read_thread",
                      "open_in_gmail", "privacy_report", "dashboard", "dashboard_feed", "mcp_config", "grok_status", "grok_enable",
                      "grok_disable", "grok_regenerate", "grok_set_keep_awake", "grok_copy_link", "grok_approve", "grok_revoke",
                      "grok_set_link_only", "grok_clear_activity", "open_connectors", "sources",
                      "connect_source", "sync_source", "disconnect_source", "open_source_settings",
                      "open_link", "mail_folders", "mail_list", "mail_thread", "mail_reasons",
                      "mail_coverage", "mail_labels", "tag_create", "tag_update", "tag_delete", "tag_thread",
                      "update_status", "update_check", "update_set_auto",
                      "update_install", "update_open_download", "update_queue", "update_cancel_queue", "update_set_on_quit", "update_later", "update_skip", "update_unskip", "review", "review_answer",
                      "review_set_scan", "review_open", "review_save_report", "permission_log",
                      "permission_log_clear", "window_focused",
                      "pdf_selection", "pdf_add_folder", "pdf_add_file", "pdf_remove", "pdf_pick",
                      "scrubber_rules", "scrubber_add", "scrubber_set_enabled", "scrubber_delete", "skipped_item", "skipped_previews", "skipped_forget",
                      "scrubber_preview"}


def test_mcp_config_points_at_this_data(backend):
    import json
    servers = json.loads(backend.mcp_config()["json"])["mcpServers"]
    assert list(servers) == ["safecontext-dev"]  # tests run from a checkout
    cfg = servers["safecontext-dev"]
    assert cfg["args"][-1] == "mcp" and str(backend._data_dir) in cfg["args"]
    assert cfg["args"][:2] == ["-m", "safecontext"]  # running from a python interpreter


def test_mcp_config_in_packaged_app_calls_the_launcher_without_dash_m(backend, monkeypatch):
    import json
    import sys

    from safecontext import env
    exe = "/Applications/SafeContext.app/Contents/MacOS/SafeContext"
    monkeypatch.setattr(sys, "executable", exe)
    monkeypatch.setattr(env, "CURRENT", env.APP)
    servers = json.loads(backend.mcp_config()["json"])["mcpServers"]
    assert list(servers) == ["safecontext"]
    cfg = servers["safecontext"]
    assert cfg["command"] == exe
    assert cfg["args"] == ["--data-dir", str(backend._data_dir), "mcp"]
    assert "env" not in cfg


def test_framework_python_app_is_not_mistaken_for_the_bundle():
    from safecontext.app.backend import _in_app_bundle
    assert not _in_app_bundle("/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python")
    assert not _in_app_bundle("/usr/local/bin/python3.12")
    assert _in_app_bundle("/Applications/SafeContext.app/Contents/MacOS/SafeContext")


def test_status_says_which_environment_is_running(backend):
    assert backend.status()["environment"] == "dev"


def test_html_is_self_contained_and_locked_down():
    html = build_html()
    assert '<link rel="stylesheet"' not in html and '<script src=' not in html
    assert "default-src 'none'" in html and "connect-src 'none'" in html and "img-src data:" in html
    js = (Path(__file__).resolve().parent.parent / "src/safecontext/app/ui/app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js


def test_demo_gmail_has_mail():
    assert len(make_fake_gmail().messages) >= 16


# --- automatic syncing: launch, every few minutes, window focus, Mac wake ------------------

from safecontext.app import backend as backend_mod  # noqa: E402


def _synced(backend):
    backend.connect("full")
    wait_sync(backend)
    return backend._last_run["gmail"]


def test_first_tick_syncs_at_launch(backend):
    _synced(backend)
    backend._last_run.clear()  # as after a fresh launch: nothing has run in this process
    assert backend._auto_tick() == ["gmail"]
    wait_sync(backend)


def test_auto_sync_waits_the_interval(backend):
    last = _synced(backend)
    backend._last_tick = last
    assert backend._auto_tick(last + 20) == []
    assert backend._auto_tick(last + backend_mod.AUTO_SYNC_SECONDS + 1) == ["gmail"]
    wait_sync(backend)


def test_wake_checks_after_the_short_gap(backend):
    last = _synced(backend)
    backend._last_tick = last
    # The clock jumped well past one tick: the Mac slept. Two and a half minutes is enough.
    assert backend._auto_tick(last + backend_mod.MIN_SYNC_GAP_SECONDS + 30) == ["gmail"]
    wait_sync(backend)


def test_window_focus_checks_unless_just_checked(backend):
    _synced(backend)
    assert backend.window_focused() == {"ok": True, "started": []}  # auto sync off (tests, CLI)
    backend._auto_sync_on = True
    assert backend.window_focused()["started"] == []  # just synced
    backend._last_run["gmail"] -= backend_mod.MIN_SYNC_GAP_SECONDS + 1
    assert backend.window_focused()["started"] == ["gmail"]
    wait_sync(backend)


def test_auto_sync_leaves_sources_that_need_the_user(backend):
    _synced(backend)
    backend._last_run.clear()
    backend._store.set_state("gmail", "auth_state", "needs_reconnect")
    assert backend._auto_tick() == []
    backend._store.set_state("gmail", "auth_state", "needs_permission")
    assert backend._auto_sync(0) == []


def test_a_finished_sync_counts_as_a_check(backend, monkeypatch):
    """A long first sync that stops (network gone) must not be retried on the very next tick."""
    _synced(backend)
    monkeypatch.setattr(backend_mod.time, "time", lambda: 10_000_000.0)
    backend._last_run["gmail"] = 0.0  # started long ago...
    backend._run_sync("gmail")  # ...and just finished
    assert backend._auto_sync(backend_mod.AUTO_SYNC_SECONDS, 10_000_000.0 + 60) == []


def test_scrubber_controls_preview_a_file_sample_without_storing_it(backend):
    import json
    ssn = "536-22-1987"
    rules = backend.scrubber_rules()
    assert rules["ok"] and any(row["kind"] == "SSN" for row in rules["builtin"])
    assert rules["patterns"] == []
    added = backend.scrubber_add("Employee ID", r"EMP-\d{4}")
    assert added["ok"] and added["patterns"][0]["kind"] == "EMPLOYEE_ID"
    refused = backend.scrubber_add("Too loose", r"a*")
    assert refused["ok"] is False and "empty" in refused["error"]
    sample = f"SSN {ssn} badge EMP-4242"
    shown = backend.scrubber_preview(sample)
    assert shown["ok"] and ssn not in shown["text"] and "EMP-4242" not in shown["text"]
    saved = json.dumps(backend.scrubber_rules())
    assert sample not in saved and ssn not in saved
    off = backend.scrubber_set_enabled(added["patterns"][0]["id"], False)
    assert off["patterns"][0]["enabled"] is False
    paused = backend.scrubber_preview(sample)
    assert paused["ok"] and "EMP-4242" in paused["text"] and ssn not in paused["text"]
    gone = backend.scrubber_delete(added["patterns"][0]["id"])
    assert gone["patterns"] == []
    removed = backend.scrubber_preview(sample)
    assert removed["ok"] and "EMP-4242" in removed["text"] and ssn not in removed["text"]
