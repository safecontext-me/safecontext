#!/usr/bin/env python3
"""Print SafeContext's anonymous usage stats from Cloudflare Workers Analytics Engine.

Reads two datasets with the Analytics Engine SQL API:
  safecontext_relay    one point per relay event (see relay/src/stats.js)
                       index1 = link token (HMAC of the link), blob1 = event, blob2 = tool,
                       blob3 = app, blob4 = outcome, double1 = 1, double2 = latency ms,
                       double3 = response size bucket (KiB)
  safecontext_updates  one point per update check (blob1 = app version), once it's live

Needs a read-only Cloudflare API token with the "Account Analytics: Read" permission, from
the SAFECONTEXT_STATS_TOKEN environment variable or the macOS Keychain (generic password,
service "safecontext-stats"). Never from a file. The account ID comes from --account or
CLOUDFLARE_ACCOUNT_ID.

    .venv/bin/python scripts/stats.py --days 14
    .venv/bin/python scripts/stats.py --days 92 --export   # also keep the history (see below)

--export also writes the numbers as JSON files to ~/.safecontext-stats (or --export DIR),
merged with what earlier exports kept, so the history outlives Analytics Engine's ~90 days.
These are the files the usage dashboard shows: daily.json, usage.json, links.json,
installs.json, each {"rows": [...], "meta": [...]}. Link tokens are cut to 12 characters.

Standard library only. Nothing is written anywhere.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

API = "https://api.cloudflare.com/client/v4/accounts/{account}/analytics_engine/sql"
KEYCHAIN_SERVICE = "safecontext-stats"
TOKEN_ENV = "SAFECONTEXT_STATS_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
RELAY_DATASET = "safecontext_relay"
UPDATES_DATASET = "safecontext_updates"
REQUEST_EVENTS = ("mcp_request", "mac_offline", "rate_limited")
DEFAULT_EXPORT_DIR = Path.home() / ".safecontext-stats"
TOKEN_CHARS = 12


class StatsError(Exception):
    pass


# --- fetching -------------------------------------------------------------------------------------
def get_token() -> str:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if tok:
        return tok
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    raise StatsError(f"No API token. Set {TOKEN_ENV}, or add it to the Keychain under the "
                     f"service \"{KEYCHAIN_SERVICE}\" (see relay/README.md, Usage stats).")


def run_sql(account: str, token: str, sql: str) -> List[dict]:
    req = urllib.request.Request(API.format(account=account), data=sql.encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise StatsError(f"Analytics Engine answered {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise StatsError(f"Couldn't reach the Analytics Engine SQL API: {e}") from None
    return list(body.get("data") or [])


def relay_sql(days: int) -> str:
    return f"""
SELECT toStartOfInterval(timestamp, INTERVAL '1' DAY) AS day, index1 AS link, blob1 AS event,
       blob2 AS tool, blob3 AS app, blob4 AS outcome,
       SUM(_sample_interval) AS n, SUM(_sample_interval * double2) AS latency_ms
FROM {RELAY_DATASET}
WHERE timestamp > NOW() - INTERVAL '{int(days)}' DAY
GROUP BY day, link, event, tool, app, outcome
ORDER BY day
LIMIT 100000
FORMAT JSON"""


def updates_sql(days: int) -> str:
    return f"""
SELECT toStartOfInterval(timestamp, INTERVAL '1' DAY) AS day, blob1 AS version, SUM(_sample_interval) AS n
FROM {UPDATES_DATASET}
WHERE timestamp > NOW() - INTERVAL '{int(days)}' DAY
GROUP BY day, version
ORDER BY day
LIMIT 10000
FORMAT JSON"""


# --- shaping --------------------------------------------------------------------------------------
def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _day(v) -> str:
    return str(v or "")[:10]


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1); 0 for no values."""
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return s[k]


def table(headers: Sequence[str], rows: Iterable[Sequence]) -> str:
    rows = [[_fmt(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = lambda cells: "  ".join(c.rjust(w) if i else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths)))
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def _fmt(c) -> str:
    if isinstance(c, float):
        return f"{c:,.0f}" if c == int(c) else f"{c:,.1f}"
    if isinstance(c, int):
        return f"{c:,}"
    return str(c)


def relay_report(rows: List[dict]) -> str:
    if not rows:
        return "Relay: no data points in this period."
    days = sorted({_day(r.get("day")) for r in rows})
    per_day: Dict[str, Counter] = defaultdict(Counter)
    per_day_link: Dict[str, Counter] = defaultdict(Counter)   # requests per link per day
    per_link: Counter = Counter()
    by_app: Dict[str, Counter] = defaultdict(Counter)
    by_tool: Counter = Counter()
    outcomes: Counter = Counter()
    latency = defaultdict(float)
    latency_n = Counter()
    for r in rows:
        d, n, ev = _day(r.get("day")), _num(r.get("n")), r.get("event") or "?"
        link, app, outcome = r.get("link") or "", r.get("app") or "other", r.get("outcome") or "?"
        if ev in REQUEST_EVENTS:
            per_day[d]["requests"] += n
            per_day_link[d][link] += n
            per_link[link] += n
            by_app[app][d] += n
            outcomes[f"{ev}:{outcome}"] += n
            if ev == "mcp_request":
                by_tool[r.get("tool") or "(none)"] += n
                if outcome == "ok":
                    per_day[d]["ok"] += n
                    latency[d] += _num(r.get("latency_ms"))
                    latency_n[d] += n
                elif outcome != "unauthorized":
                    per_day[d]["errors"] += n
                else:
                    per_day[d]["unauthorized"] += n
            elif ev == "mac_offline":
                per_day[d]["offline"] += n
            else:
                per_day[d]["rate_limited"] += n
        elif ev == "oauth_connect":
            per_day[d]["connects"] += n
            by_app[app]["connects"] += n

    out = ["RELAY, per day (UTC)"]
    daily = []
    for d in days:
        c, links = per_day[d], list(per_day_link[d].values())
        daily.append([d, len(links), c["requests"], c["ok"], c["errors"], c["unauthorized"], c["offline"],
                      c["rate_limited"], c["connects"], statistics.median(links) if links else 0,
                      percentile(links, 0.9), latency[d] / latency_n[d] if latency_n[d] else 0])
    out.append(table(["day", "active links", "requests", "ok", "errors", "unauth", "offline", "rate ltd",
                      "connects", "req/link med", "req/link p90", "ok latency ms"], daily))

    totals = list(per_link.values())
    out += ["", f"PER LINK, whole period: {len(totals)} links, median {statistics.median(totals) if totals else 0:,.0f}"
                f" requests, p90 {percentile(totals, 0.9):,.0f}", "Top 10 (token truncated):"]
    out.append(table(["link token", "requests"], [[k[:12] + "…" if k else "(none)", v] for k, v in per_link.most_common(10)]))

    apps = sorted(by_app)
    out += ["", "REQUESTS BY APP, per day"]
    out.append(table(["day", *apps], [[d, *(by_app[a][d] for a in apps)] for d in days]))
    out.append(table(["sign-ins (connects)", *apps], [["whole period", *(by_app[a]["connects"] for a in apps)]]))

    out += ["", "MCP REQUESTS BY TOOL OR METHOD, whole period"]
    out.append(table(["tool", "requests"], by_tool.most_common()))
    out += ["", "OUTCOMES, whole period"]
    out.append(table(["event:outcome", "count"], outcomes.most_common()))
    return "\n".join(out)


def updates_report(rows: List[dict]) -> str:
    if not rows:
        return "INSTALLS: no update checks recorded yet."
    days = sorted({_day(r.get("day")) for r in rows})
    versions = sorted({r.get("version") or "unknown" for r in rows}, key=_version_key)
    grid: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        grid[_day(r.get("day"))][r.get("version") or "unknown"] += _num(r.get("n"))
    lines = ["INSTALLS (update checks per day, by app version; roughly one per running install per day)"]
    lines.append(table(["day", *versions, "total"],
                       [[d, *(grid[d][v] for v in versions), sum(grid[d].values())] for d in days]))
    return "\n".join(lines)


def _version_key(v: str):
    try:
        return (0, tuple(int(x) for x in v.split(".")))
    except ValueError:
        return (1, ())


# --- export ---------------------------------------------------------------------------------------
def daily_rows(rows: List[dict]) -> List[dict]:
    """One row per day: the same figures as the first relay table."""
    days: Dict[str, Counter] = defaultdict(Counter)
    per_link: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        d, n, ev, outcome = _day(r.get("day")), _num(r.get("n")), r.get("event"), r.get("outcome")
        c = days[d]
        if ev == "oauth_connect":
            c["connects"] += n
            continue
        if ev not in REQUEST_EVENTS:
            continue
        c["requests"] += n
        per_link[d][r.get("link") or ""] += n
        if ev == "mac_offline":
            c["timeouts" if outcome == "timeout" else "offline"] += n
        elif ev == "rate_limited":
            c["rate_limited"] += n
        elif outcome == "ok":
            c["ok"] += n
            c["latency_sum"] += _num(r.get("latency_ms"))
        elif outcome == "unauthorized":
            c["unauthorized"] += n
        else:
            c["errors"] += n
    out = []
    for d in sorted(days):
        c, links = days[d], list(per_link[d].values())
        out.append({
            "day": d, "active_links": len(links), "requests": int(c["requests"]), "ok": int(c["ok"]),
            "errors": int(c["errors"]), "unauthorized": int(c["unauthorized"]), "offline": int(c["offline"]),
            "timeouts": int(c["timeouts"]), "rate_limited": int(c["rate_limited"]), "connects": int(c["connects"]),
            "req_per_link_median": float(statistics.median(links)) if links else 0.0,
            "req_per_link_p90": float(percentile(links, 0.9)),
            "ok_latency_ms": round(c["latency_sum"] / c["ok"], 1) if c["ok"] else 0.0,
        })
    return out


def usage_rows(rows: List[dict]) -> List[dict]:
    """Counts by day, app, tool, event and outcome (no link)."""
    agg: Counter = Counter()
    for r in rows:
        key = (_day(r.get("day")), r.get("app") or "other", r.get("tool") or "", r.get("event") or "", r.get("outcome") or "")
        agg[key] += _num(r.get("n"))
    return [{"day": k[0], "app": k[1], "tool": k[2], "event": k[3], "outcome": k[4], "count": int(v)}
            for k, v in sorted(agg.items())]


def link_rows(rows: List[dict]) -> List[dict]:
    """Requests per link per day, with the token cut short."""
    agg: Counter = Counter()
    for r in rows:
        if r.get("event") in REQUEST_EVENTS:
            agg[(_day(r.get("day")), (r.get("link") or "")[:TOKEN_CHARS])] += _num(r.get("n"))
    return [{"day": d, "link": l, "requests": int(v)} for (d, l), v in sorted(agg.items())]


def install_rows(rows: List[dict]) -> List[dict]:
    agg: Counter = Counter()
    for r in rows:
        agg[(_day(r.get("day")), r.get("version") or "unknown")] += _num(r.get("n"))
    return [{"day": d, "version": v, "checks": int(n)} for (d, v), n in sorted(agg.items())]


def merge_days(old: List[dict], new: List[dict], window_start: str) -> List[dict]:
    """Keep old rows for days before the queried window, and for its first (partial) day when
    the new query has none; the new rows win for every later day."""
    new_days = {r["day"] for r in new}
    kept = [r for r in old if r.get("day", "") < window_start
            or (r.get("day") == window_start and window_start not in new_days)]
    return sorted(kept + new, key=lambda r: tuple(str(r.get(k, "")) for k in ("day", "app", "tool", "event", "outcome", "link", "version")))


def export(out_dir: Path, days: int, relay: List[dict], installs: Optional[List[dict]],
           now: Optional[datetime] = None) -> List[Path]:
    now = now or datetime.now(timezone.utc)
    window_start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    files = {
        "daily.json": (daily_rows(relay), relay_sql(days)),
        "usage.json": (usage_rows(relay), relay_sql(days)),
        "links.json": (link_rows(relay), relay_sql(days)),
    }
    if installs is not None:
        files["installs.json"] = (install_rows(installs), updates_sql(days))
    written = []
    for name, (rows, sql) in files.items():
        path = out_dir / name
        old: List[dict] = []
        if path.exists():
            try:
                old = list(json.loads(path.read_text()).get("rows") or [])
            except (OSError, ValueError, AttributeError):
                old = []
        merged = merge_days(old, rows, window_start)
        meta = [
            {"label": "Made by", "value": "scripts/stats.py --export"},
            {"label": "Exported", "value": stamp},
            {"label": "Query", "value": sql.strip(), "type": "code", "lang": "sql"},
            {"label": "Kept from earlier exports", "value": f"days before {window_start}"},
        ]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rows": merged, "meta": meta}, indent=0))
        tmp.replace(path)
        written.append(path)
    return written


# --- main -----------------------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SafeContext anonymous usage stats (Cloudflare Analytics Engine).")
    ap.add_argument("--days", type=int, default=14, help="how many days back (default 14; data is kept ~90)")
    ap.add_argument("--account", default=os.environ.get(ACCOUNT_ENV, ""),
                    help=f"Cloudflare account ID (default: ${ACCOUNT_ENV})")
    ap.add_argument("--export", nargs="?", const=str(DEFAULT_EXPORT_DIR), default=None, metavar="DIR",
                    help=f"also write JSON files for the dashboard, merged with earlier ones (default {DEFAULT_EXPORT_DIR})")
    args = ap.parse_args(argv)
    if not args.account:
        print(f"Give the Cloudflare account ID with --account or {ACCOUNT_ENV}.", file=sys.stderr)
        return 2
    if not 1 <= args.days <= 92:
        print("--days must be between 1 and 92.", file=sys.stderr)
        return 2
    try:
        token = get_token()
        relay = run_sql(args.account, token, relay_sql(args.days))
        print(relay_report(relay))
        print()
        installs: Optional[List[dict]] = None
        try:
            installs = run_sql(args.account, token, updates_sql(args.days))
            print(updates_report(installs))
        except StatsError:
            print("INSTALLS: not available yet (the update-check counter isn't live).")
        if args.export:
            paths = export(Path(args.export).expanduser(), args.days, relay, installs)
            print()
            print(f"Exported {len(paths)} files to {Path(args.export).expanduser()}")
    except StatsError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
