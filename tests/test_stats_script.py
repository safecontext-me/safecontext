"""scripts/stats.py: report shaping and token lookup, with fixture rows only (no network)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "stats_script", Path(__file__).resolve().parents[1] / "scripts" / "stats.py")
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)

A, B, C = "a" * 64, "b" * 64, "c" * 64


def row(day, link, event, tool="", app="grok", outcome="ok", n=1, latency=0):
    # Shaped like the SQL API's JSON (64-bit sums come back as strings).
    return {"day": f"{day} 00:00:00", "link": link, "event": event, "tool": tool, "app": app,
            "outcome": outcome, "n": str(n), "latency_ms": str(latency)}


ROWS = [
    row("2026-11-23", A, "oauth_connect"),
    row("2026-11-23", A, "mcp_request", "search", n=10, latency=1000),
    row("2026-11-23", B, "mcp_request", "read", app="claude", n=2, latency=400),
    row("2026-11-23", B, "mac_offline", "search", app="claude", outcome="offline", n=1),
    row("2026-11-24", A, "mcp_request", "search", n=4, latency=400),
    row("2026-11-24", C, "mcp_request", app="other", outcome="unauthorized", n=3),
    row("2026-11-24", C, "rate_limited", app="other", outcome="rate_limited", n=1),
]


def test_relay_report_counts():
    out = stats.relay_report(ROWS)
    lines = out.splitlines()
    day1 = next(l for l in lines if l.startswith("2026-11-23") and "13" in l)
    # active links 2, requests 13 (10 + 2 + 1 offline), ok 12, offline 1, connects 1, avg ok latency 1400/12
    assert day1.split()[:9] == ["2026-11-23", "2", "13", "12", "0", "0", "1", "0", "1"]
    assert "116.7" in day1
    day2 = next(l for l in lines if l.startswith("2026-11-24") and "unauth" not in l)
    assert day2.split()[:8] == ["2026-11-24", "2", "8", "4", "0", "3", "0", "1"]
    assert "PER LINK, whole period: 3 links, median 4 requests" in out
    assert A[:12] + "…" in out and A not in out  # tokens are truncated
    assert "search" in out and "read" in out
    assert "mac_offline:offline" in out


def test_percentile_and_empty():
    assert stats.percentile([], 0.9) == 0
    assert stats.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    assert stats.percentile([5], 0.5) == 5
    assert "no data points" in stats.relay_report([])
    assert "no update checks" in stats.updates_report([])


def test_updates_report_orders_versions():
    rows = [{"day": "2026-11-24 00:00:00", "version": v, "n": n}
            for v, n in [("0.1.10", "2"), ("0.1.5", "7"), ("unknown", "1")]]
    out = stats.updates_report(rows)
    header = out.splitlines()[1].split()
    assert header == ["day", "0.1.5", "0.1.10", "unknown", "total"]
    assert out.splitlines()[3].split() == ["2026-11-24", "7", "2", "1", "10"]


def test_sql_is_bounded():
    assert "INTERVAL '14' DAY" in stats.relay_sql(14)
    assert "FROM safecontext_relay" in stats.relay_sql(3)
    assert "FROM safecontext_updates" in stats.updates_sql(3)


def test_token_from_env_never_a_file(monkeypatch):
    monkeypatch.setenv(stats.TOKEN_ENV, " tok123 ")
    assert stats.get_token() == "tok123"


def test_missing_token_is_a_clear_error(monkeypatch):
    monkeypatch.delenv(stats.TOKEN_ENV, raising=False)

    class NoKeychain:
        returncode, stdout = 44, ""

    monkeypatch.setattr(stats.subprocess, "run", lambda *a, **k: NoKeychain())
    with pytest.raises(stats.StatsError, match="SAFECONTEXT_STATS_TOKEN"):
        stats.get_token()


def test_needs_account(monkeypatch, capsys):
    monkeypatch.delenv(stats.ACCOUNT_ENV, raising=False)
    assert stats.main([]) == 2
    assert "account ID" in capsys.readouterr().err


def test_export_writes_and_merges(tmp_path):
    from datetime import datetime, timezone
    installs = [{"day": "2026-11-24 00:00:00", "version": "0.1.10", "n": "5"}]
    paths = stats.export(tmp_path, 7, ROWS, installs, now=datetime(2026, 11, 27, 12, tzinfo=timezone.utc))
    assert sorted(p.name for p in paths) == ["daily.json", "installs.json", "links.json", "usage.json"]
    import json
    daily = json.loads((tmp_path / "daily.json").read_text())["rows"]
    assert [d["day"] for d in daily] == ["2026-11-23", "2026-11-24"]
    assert daily[0]["requests"] == 13 and daily[0]["offline"] == 1 and daily[0]["connects"] == 1
    assert daily[1]["rate_limited"] == 1 and daily[1]["unauthorized"] == 3
    links = json.loads((tmp_path / "links.json").read_text())["rows"]
    assert all(len(r["link"]) == 12 for r in links)
    # A later export with a 2-day window keeps the older days and replaces the newer ones.
    later = [row("2026-11-27", A, "mcp_request", "search", n=2)]
    stats.export(tmp_path, 2, later, None, now=datetime(2026, 11, 28, 12, tzinfo=timezone.utc))
    daily = json.loads((tmp_path / "daily.json").read_text())["rows"]
    assert [d["day"] for d in daily] == ["2026-11-23", "2026-11-24", "2026-11-27"]
    assert json.loads((tmp_path / "installs.json").read_text())["rows"][0]["checks"] == 5  # untouched
