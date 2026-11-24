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
from typing import Dict, Iterable, List, Optional, Sequence

API = "https://api.cloudflare.com/client/v4/accounts/{account}/analytics_engine/sql"
KEYCHAIN_SERVICE = "safecontext-stats"
TOKEN_ENV = "SAFECONTEXT_STATS_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
RELAY_DATASET = "safecontext_relay"
UPDATES_DATASET = "safecontext_updates"
REQUEST_EVENTS = ("mcp_request", "mac_offline", "rate_limited")


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


# --- main -----------------------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SafeContext anonymous usage stats (Cloudflare Analytics Engine).")
    ap.add_argument("--days", type=int, default=14, help="how many days back (default 14; data is kept ~90)")
    ap.add_argument("--account", default=os.environ.get(ACCOUNT_ENV, ""),
                    help=f"Cloudflare account ID (default: ${ACCOUNT_ENV})")
    args = ap.parse_args(argv)
    if not args.account:
        print(f"Give the Cloudflare account ID with --account or {ACCOUNT_ENV}.", file=sys.stderr)
        return 2
    if not 1 <= args.days <= 92:
        print("--days must be between 1 and 92.", file=sys.stderr)
        return 2
    try:
        token = get_token()
        print(relay_report(run_sql(args.account, token, relay_sql(args.days))))
        print()
        try:
            print(updates_report(run_sql(args.account, token, updates_sql(args.days))))
        except StatsError:
            print("INSTALLS: not available yet (the update-check counter isn't live).")
    except StatsError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
