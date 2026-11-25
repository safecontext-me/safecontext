"""AI access review: what you've granted to AI apps, how risky it is, and how to fix it.

The app shows this as "AI access review". It mixes three kinds of checks:

  * automatic: SafeContext's own grants (Google scopes, the MCP link, link-only mode,
    sign-ins AI apps hold, Full Disk Access for iMessage), read from SafeContext's
    own state. Nothing new is read.
  * opt-in scan: on the user's click, a few fixed config files other AI tools keep in
    the home folder (Claude Desktop's MCP config, Claude Code's settings). Only names,
    modes and permission rules are read. Environment values and arguments, which often
    hold API keys, are never returned or stored. Nothing here is in a folder macOS
    protects (no Documents/Desktop/Downloads, no app containers), so there's no prompt.
  * guided: pages only the user can open (Google, Grok, Claude, X, GitHub, System
    Settings). The user opens each one and records what they found; answers expire
    after REVIEW_EXPIRE_DAYS so the review comes back around.

Server-side grants at xAI or Anthropic have no API, and no app can read another app's
macOS privacy grants (TCC.db needs Full Disk Access; Apple offers no API), so those are
guided checks rather than detections.

Statuses, worst first: risk (fix this), warn (worth a look), todo (not checked yet),
info, ok. The app and report show them as red "Fix", yellow "Worth a look", grey "Not checked
yet" and green "OK", grouped by app or account (Grok, Claude, Google, GitHub, SafeContext,
This Mac). group_of, colour_of, rollup and summarize are pure so they can be tested alone.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

RISK, WARN, TODO, INFO, OK = "risk", "warn", "todo", "info", "ok"
STATUS_ORDER = {RISK: 0, WARN: 1, TODO: 2, INFO: 3, OK: 4}
ANSWERS = ("ok", "fixed", "issue", "skip")
REVIEW_EXPIRE_DAYS = 90
LINK_IDLE_DAYS = 30
DAY_MS = 86_400_000

META_ANSWERS = "review_answers"
META_SCAN = "review_scan"

# Grouped by the app or account people think in, in a fixed order so answering a check never moves
# a group out from under the pointer. The summary at the top says which groups need attention.
GROUPS = [
    ("grok", "Grok", "Connectors, data controls, and Grok on X."),
    ("claude", "Claude", "claude.ai, Claude in Chrome, Claude Desktop and Claude Code."),
    ("google", "Google", "Apps with access to your Google account, including SafeContext."),
    ("github", "GitHub", "Apps and tokens that can read or change your code."),
    ("safecontext", "SafeContext", "The MCP link Grok, Claude and other AI apps use to reach this Mac."),
    ("mac", "This Mac", "macOS privacy permissions that let apps read files, see the screen or control the Mac."),
]
GROUP_IDS = [g[0] for g in GROUPS]
_GROUP_OF = {
    "grok_connectors": "grok", "grok_data": "grok", "x_grok": "grok",
    "claude_connectors": "claude", "claude_data": "claude", "claude_chrome": "claude",
    "google_connections": "google", "sc_gmail": "google", "sc_gcal": "google",
    "github_apps": "github",
    "sc_link": "safecontext", "sc_link_only": "safecontext", "sc_awake": "safecontext",
    "sc_fda": "mac", "mac_full_disk": "mac", "mac_accessibility": "mac", "mac_screen": "mac",
    "mac_automation": "mac",
}

# Status → what the user sees. Colour is always paired with a word and an icon shape.
RED, YELLOW, GREEN, GREY = "red", "yellow", "green", "grey"
COLOUR_OF = {RISK: RED, WARN: YELLOW, TODO: GREY, INFO: GREEN, OK: GREEN}
# Worst first. Grey ("not checked yet") ranks above green so an unchecked group never looks safe.
COLOUR_ORDER = [RED, YELLOW, GREY, GREEN]
COLOUR_LABEL = {RED: "Fix", YELLOW: "Worth a look", GREEN: "OK", GREY: "Not checked yet"}


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str = ""
    fix: str = ""
    link: str = ""         # a fixed page from GUIDED (never anything read from a file)
    manual: bool = False   # the user answers it
    answer: str = ""       # ok | fixed | issue | skip
    answered_ms: Optional[int] = None
    items: List[str] = field(default_factory=list)  # names to list under it (server names, rules)
    group: str = ""        # set by build_review from group_of()
    colour: str = ""       # set by build_review from colour_of()
    label: str = ""        # the word shown with the colour


# --- guided checks --------------------------------------------------------------------------
# (id, title, why it matters, what to do, link). The group comes from group_of(). Links are the official pages; System
# Settings links use the x-apple.systempreferences scheme and only open the right pane.

_PANE = "x-apple.systempreferences:com.apple.preference.security?"
GUIDED = [
    ("google_connections", "Apps with access to your Google account",
     "Apps you let into Gmail, Calendar or Drive keep that access until you remove it, even after you "
     "stop using them or disconnect them inside Claude or Grok.",
     "Remove any app you don't recognize or no longer use: open it → See details → Remove access.",
     "https://myaccount.google.com/connections"),
    ("grok_connectors", "Grok connectors",
     "A connector lets Grok read that account on your behalf. Gmail can include sending and changing mail "
     "if it was turned on.",
     "Disconnect any connector you don't use.",
     "https://grok.com/connectors"),
    ("grok_data", "Grok data controls",
     "Training and memory decide how long your chats are kept and what they're used for.",
     "In grok.com, open your profile → Settings → Data controls. Check \"Improve the model\", memory, "
     "and unshare old shared links.",
     "https://grok.com/"),
    ("x_grok", "X: Grok training and connected apps",
     "X lets Grok train on your posts and Grok chats by default. Connected apps can act on your X account.",
     "Settings → Privacy and safety → Grok & Third-party collaborators: untick training if you want. "
     "Then Security and account access → Apps and sessions → Connected apps: revoke what you don't use.",
     "https://x.com/settings/connected_apps"),
    ("claude_connectors", "claude.ai connectors and tool permissions",
     "Connectors let Claude read and sometimes act in other accounts. A tool set to \"Always allow\" runs "
     "without asking, which matters most for tools that send, post or write.",
     "Remove connectors you don't use. For the rest, set tools that send, post or write to \"Needs approval\".",
     "https://claude.ai/customize/connectors/yours"),
    ("claude_data", "claude.ai memory, training and shared chats",
     "With \"Help improve Claude\" on, chats can be kept for up to 5 years. Shared links stay public until "
     "you unshare them. Deleting a chat doesn't delete memories made from it.",
     "Settings → Memory: review what it remembers. Settings → Privacy: check the training toggle and "
     "Manage shared chats.",
     "https://claude.ai/settings"),
    ("claude_chrome", "Claude in Chrome: approved sites",
     "Sites set to \"always allow\" let Claude click and type as you there, where you're signed in.",
     "Click the extension icon → ⋯ → Extension settings → Your approved sites. Remove mail, banking and "
     "admin sites. Skip this if you don't use the extension.",
     ""),
    ("github_apps", "Authorized apps and tokens",
     "OAuth apps, GitHub Apps and personal access tokens can read or change your code.",
     "Revoke apps you don't use (and check Installed apps too: uninstalling doesn't revoke). Delete tokens "
     "that are old or unused. Skip this if you don't use GitHub.",
     "https://github.com/settings/applications"),
    ("mac_full_disk", "Full Disk Access",
     "Apps here can read everything in your home folder, including Messages and Mail.",
     "Turn off anything you don't need, especially Terminal or AI apps you no longer use.",
     _PANE + "Privacy_AllFiles"),
    ("mac_accessibility", "Accessibility",
     "Apps here can control your Mac: click, type and read what's on screen in other apps.",
     "Turn off AI or automation apps you don't use.",
     _PANE + "Privacy_Accessibility"),
    ("mac_screen", "Screen & System Audio Recording",
     "Apps here can see everything on your screen.",
     "Turn off anything you don't recognize or no longer use.",
     _PANE + "Privacy_ScreenCapture"),
    ("mac_automation", "Automation",
     "Apps here can control other apps, such as Mail, Messages or Finder, through scripts.",
     "Untick pairs you don't recognize.",
     _PANE + "Privacy_Automation"),
]
GUIDED_BY_ID = {g[0]: g for g in GUIDED}


def guided_link(check_id: str) -> Optional[str]:
    g = GUIDED_BY_ID.get(check_id)
    return g[4] if g and g[4] else None


def load_answers(raw: Optional[str]) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if k in GUIDED_BY_ID and isinstance(v, dict) and v.get("answer") in ANSWERS}


def guided_checks(answers: Dict[str, Dict[str, Any]], now_ms: int) -> List[Check]:
    out = []
    for cid, title, why, fix, link in GUIDED:
        a = answers.get(cid) or {}
        ts = a.get("ts")
        fresh = isinstance(ts, (int, float)) and now_ms - ts < REVIEW_EXPIRE_DAYS * DAY_MS
        answer = a.get("answer", "") if fresh else ""
        status = {"ok": OK, "fixed": OK, "skip": INFO, "issue": RISK}.get(answer, TODO)
        detail = why
        if answer == "fixed":
            detail = "You fixed something here. " + why
        elif answer == "skip":
            detail = "You don't use this. " + why
        elif answer == "issue":
            detail = "You found something to fix here. " + why
        out.append(Check(cid, title, status, detail, fix, link, manual=True, answer=answer,
                         answered_ms=int(ts) if fresh else None))
    return out


# --- SafeContext's own grants -----------------------------------------------------------------

def _ago(ms: int, now_ms: int) -> str:
    days = (now_ms - ms) // DAY_MS
    return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")


def safecontext_checks(sources: Iterable[Dict[str, Any]], mcp: Dict[str, Any], now_ms: int) -> List[Check]:
    """`sources`: Sources.statuses() rows. `mcp`: GrokLink.status(). Only facts SafeContext keeps."""
    out: List[Check] = []
    by_id = {s["id"]: s for s in sources if s.get("available")}
    gmail = by_id.get("gmail") or {}
    if gmail.get("account"):
        headers = gmail.get("mode") == "headers"
        out.append(Check("sc_gmail", "SafeContext's Gmail access: read-only", OK,
                         "SafeContext can only read " + ("headers (sender, subject, date)" if headers else "your mail")
                         + ". It can't send, change or delete anything. Google lists it under your "
                           "account's third-party connections.",
                         "To remove it: Sources → Gmail → Disconnect, which also revokes it at Google."))
    gcal = by_id.get("gcal") or {}
    if gcal.get("account"):
        out.append(Check("sc_gcal", "SafeContext's Calendar access: read-only", OK,
                         "SafeContext can read your events. It can't create, change or delete them.",
                         "To remove it: Sources → Google Calendar → Disconnect."))
    imsg = by_id.get("imessage") or {}
    if imsg.get("account"):
        out.append(Check("sc_fda", "SafeContext's Full Disk Access (for iMessage)", INFO,
                         "SafeContext needs Full Disk Access to read Messages. That access covers your whole "
                         "home folder, though SafeContext only opens the Messages database.",
                         "If you stop using iMessage here, disconnect it and turn SafeContext off in "
                         "System Settings → Privacy & Security → Full Disk Access."))

    enabled = bool(mcp.get("enabled"))
    grants = int(mcp.get("grants") or 0)
    last = mcp.get("last_used_ms")
    if not enabled:
        out.append(Check("sc_link", "MCP link: off", OK,
                         "AI apps in the cloud can't reach SafeContext. Local apps set up with the MCP "
                         "config (like Claude Desktop) still can while SafeContext is installed."))
    else:
        idle = (last is None) or (now_ms - int(last) > LINK_IDLE_DAYS * DAY_MS)
        used = f"Last used {_ago(int(last), now_ms)}." if last else "No AI app has used it yet."
        signins = f"{grants} sign-in{'s' if grants != 1 else ''} held by AI apps." if grants else \
            "No AI app is signed in."
        if idle:
            out.append(Check("sc_link", "MCP link: on but not used lately", WARN,
                             f"AI apps can search your scrubbed data while SafeContext is open. {used} {signins}",
                             "If you don't use it, turn MCP off. To sign every AI app out, use Regenerate link "
                             "on the MCP screen."))
        else:
            out.append(Check("sc_link", "MCP link: on", INFO,
                             f"AI apps can search your scrubbed data while SafeContext is open. {used} {signins}",
                             "Check the Activity list on the MCP screen for anything you don't recognize."))
        if mcp.get("link_only"):
            out.append(Check("sc_link_only", "Link-only mode is on", RISK,
                             "Anyone who has your link can read your scrubbed data without approval. Links "
                             "end up in chat histories, screenshots and shared notes.",
                             "Turn off \"Connect with just the link\" on the MCP screen, then reconnect each app "
                             "with Approve. If the link may have leaked, Regenerate it."))
        if mcp.get("keep_awake"):
            out.append(Check("sc_awake", "Keep awake is on", INFO,
                             "Your Mac stays awake on power so AI apps can reach SafeContext at any time.",
                             "Turn it off on the MCP screen if you only use AI apps while you're at the Mac."))
    return out


# --- opt-in scan of other AI tools' config ------------------------------------------------------
# Fixed paths only. Never parse anything else, never return env values or args.

CLAUDE_DESKTOP_CONFIG = Path("Library/Application Support/Claude/claude_desktop_config.json")
CLAUDE_CODE_SETTINGS = Path(".claude/settings.json")
CLAUDE_CODE_STATE = Path(".claude.json")
OWN_SERVERS = {"safecontext", "safecontext-dev"}
MAX_CONFIG_BYTES = 5 * 1024 * 1024
_SECRET_NAME = re.compile(r"key|token|secret|password|passwd|credential|auth", re.I)

# Claude Code allow rules that hand over a whole tool, or a command that can run anything
# or send data anywhere.
_WHOLE_TOOL = {"Bash", "WebFetch", "Write", "Edit", "MultiEdit", "NotebookEdit"}
_ANY_SPEC = {"", "*", ":*", "**"}
_POWERFUL_CMDS = {"curl", "wget", "ssh", "scp", "rsync", "nc", "ncat", "sudo", "rm", "python", "python3",
                  "node", "npx", "bash", "sh", "zsh", "osascript", "eval", "git push", "gh"}


def _read_json(path: Path) -> Optional[Any]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _servers(block: Any) -> List[Dict[str, Any]]:
    """MCP server entries → name, command basename, whether it holds secret-looking env values."""
    out = []
    if not isinstance(block, dict):
        return out
    for name, cfg in block.items():
        if not isinstance(name, str):
            continue
        cfg = cfg if isinstance(cfg, dict) else {}
        cmd = cfg.get("command")
        kind = "remote" if cfg.get("url") or cfg.get("type") in ("http", "sse") else "local"
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
        secret = any(_SECRET_NAME.search(str(k)) and v for k, v in {**env, **headers}.items())
        out.append({"name": name[:80], "kind": kind,
                    "command": Path(cmd).name[:60] if isinstance(cmd, str) and cmd else "",
                    "secrets": bool(secret), "own": name in OWN_SERVERS})
    return out


def broad_rule(rule: str) -> Optional[str]:
    """RISK or WARN for an allow rule that hands over too much, else None."""
    m = re.fullmatch(r"\s*([A-Za-z_]+)(?:\((.*)\))?\s*", rule)
    if not m:
        return None
    tool, spec = m.group(1), (m.group(2) or "").strip()
    if tool in _WHOLE_TOOL and spec in _ANY_SPEC:
        return RISK if tool == "Bash" else WARN
    if tool == "Bash":
        cmd = re.sub(r"(:\*|\s\*|\*)$", "", spec).strip()
        if cmd in _POWERFUL_CMDS or cmd.split(" ")[0] in {"sudo", "curl", "wget", "ssh", "nc", "osascript", "eval"}:
            return WARN
    return None


def scan_ai_configs(home: Path) -> Dict[str, Any]:
    home = Path(home)
    found: Dict[str, Any] = {"claude_desktop": None, "claude_code": None}
    desktop = _read_json(home / CLAUDE_DESKTOP_CONFIG)
    if isinstance(desktop, dict):
        found["claude_desktop"] = {"servers": _servers(desktop.get("mcpServers"))}
    settings = _read_json(home / CLAUDE_CODE_SETTINGS)
    state = _read_json(home / CLAUDE_CODE_STATE)
    if isinstance(settings, dict) or isinstance(state, dict):
        perms = settings.get("permissions") if isinstance(settings, dict) else None
        perms = perms if isinstance(perms, dict) else {}
        allow = [r for r in perms.get("allow") or [] if isinstance(r, str)]
        mode = perms.get("defaultMode") if isinstance(perms.get("defaultMode"), str) else ""
        skip_prompt = bool(isinstance(settings, dict) and settings.get("skipDangerousModePermissionPrompt"))
        servers = _servers(state.get("mcpServers")) if isinstance(state, dict) else []
        project_servers = set()
        if isinstance(state, dict) and isinstance(state.get("projects"), dict):
            for p in state["projects"].values():
                if isinstance(p, dict):
                    project_servers.update(s["name"] for s in _servers(p.get("mcpServers")))
        found["claude_code"] = {
            "mode": mode[:40], "skip_prompt": skip_prompt,
            "broad": [{"rule": r[:120], "level": broad_rule(r)} for r in allow if broad_rule(r)],
            "allow_count": len(allow), "servers": servers,
            "project_servers": sorted(project_servers - {s["name"] for s in servers})[:50]}
    return found


def local_checks(scan: Optional[Dict[str, Any]]) -> List[Check]:
    if scan is None:
        return []
    out: List[Check] = []
    d = scan.get("claude_desktop")
    if d is None:
        out.append(Check("lc_desktop", "Claude Desktop: no MCP config found", OK,
                         "Either Claude Desktop isn't installed or it has no local MCP servers."))
    else:
        others = [s for s in d["servers"] if not s["own"]]
        own = any(s["own"] for s in d["servers"])
        names = [s["name"] + (f" ({s['command']})" if s["command"] else "") for s in others]
        detail = ("Each local MCP server is a program Claude starts on your Mac. It runs as you, with "
                  "everything you can access.")
        if own:
            detail += " SafeContext is one of them."
        out.append(Check("lc_desktop", f"Claude Desktop: {len(others)} other MCP server{'s' if len(others) != 1 else ''}",
                         WARN if others else OK, detail if others or own else "No local MCP servers.",
                         "Remove any you don't use: Claude → Settings → Developer → Edit Config, delete its "
                         "entry, then restart Claude." if others else "", items=names))
        leaky = [s["name"] for s in d["servers"] if s["secrets"]]
        if leaky:
            out.append(Check("lc_desktop_secrets", "Claude Desktop: keys stored in plain text", WARN,
                             "These servers keep an API key or token in the config file, where any app that can "
                             "read your home folder can copy it. SafeContext didn't read the values.",
                             "Use a short-lived or narrowly scoped key, and remove servers you don't use.",
                             items=leaky))
    c = scan.get("claude_code")
    if c is None:
        out.append(Check("lc_code", "Claude Code: no settings found", OK,
                         "Either Claude Code isn't installed or it uses its defaults."))
        return out
    mode = c["mode"]
    if mode == "bypassPermissions" or c["skip_prompt"]:
        out.append(Check("lc_code_mode", "Claude Code: skips permission prompts", RISK,
                         "Claude Code is set to run commands and edit files without asking. A prompt injection "
                         "in a web page, issue or file it reads can then act on your Mac.",
                         "Remove \"defaultMode\": \"bypassPermissions\" (and skipDangerousModePermissionPrompt) "
                         "from ~/.claude/settings.json. Turn it on per session only when you need it."))
    elif mode == "auto":
        out.append(Check("lc_code_mode", "Claude Code: auto mode by default", WARN,
                         "A classifier approves most actions for you. That's safer than skipping prompts, but "
                         "you won't see most of what runs.",
                         "Consider the default mode for repositories you don't fully trust."))
    else:
        out.append(Check("lc_code_mode", "Claude Code: asks before acting", OK,
                         f"Default mode: {mode or 'default'}."))
    broad = c["broad"]
    if broad:
        worst = RISK if any(b["level"] == RISK for b in broad) else WARN
        out.append(Check("lc_code_allow", f"Claude Code: {len(broad)} broad \"always allow\" rule"
                         + ("s" if len(broad) != 1 else ""), worst,
                         "These rules in ~/.claude/settings.json let Claude run whole tools, or commands that can "
                         "send data out or run anything, without asking.",
                         "Narrow them to the exact commands you need, or remove them. Project rules saved with "
                         "\"don't ask again\" are in each project's .claude/settings.local.json; SafeContext "
                         "doesn't open your project folders.",
                         items=[b["rule"] for b in broad[:10]]))
    else:
        out.append(Check("lc_code_allow", "Claude Code: no broad global allow rules", OK,
                         f"{c['allow_count']} global allow rule{'s' if c['allow_count'] != 1 else ''}, none broad. "
                         "Project rules live in each project's .claude/settings.local.json, which SafeContext "
                         "doesn't open."))
    servers = [s for s in c["servers"] if not s["own"]]
    names = [s["name"] for s in servers] + [n + " (one project)" for n in c["project_servers"]
                                            if n not in OWN_SERVERS]
    if names:
        out.append(Check("lc_code_mcp", f"Claude Code: {len(names)} MCP server"
                         + ("s" if len(names) != 1 else ""), WARN,
                         "Each MCP server can give Claude new abilities. Local ones run as you.",
                         "Run claude mcp list in a terminal, and claude mcp remove <name> for any you don't use.",
                         items=names[:20]))
    return out


# --- the whole review -----------------------------------------------------------------------------

def group_of(check_id: str) -> str:
    """Which app or account a check belongs to. The opt-in scan's checks (lc_*) are all Claude's."""
    if check_id.startswith("lc_"):
        return "claude"
    return _GROUP_OF.get(check_id, "safecontext")


def colour_of(status: str) -> str:
    return COLOUR_OF.get(status, GREY)


def label_of(status: str, answer: str = "") -> str:
    """The word shown next to the colour. A "don't use this" answer is green but says so."""
    if answer == "skip":
        return "Not used"
    return COLOUR_LABEL[colour_of(status)]


def worst_colour(colours: Iterable[str]) -> str:
    """Worst of the colours given; GREEN when there are none."""
    ranked = [COLOUR_ORDER.index(c) for c in colours if c in COLOUR_ORDER]
    return COLOUR_ORDER[min(ranked)] if ranked else GREEN


def colour_counts(checks: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {c: 0 for c in COLOUR_ORDER}
    for c in checks:
        counts[colour_of(c["status"])] += 1
    return counts


def rollup(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A group header: its worst colour, counts per colour, and whether the user said they don't use
    any of it (every check answered "skip"), in which case the UI collapses it."""
    counts = colour_counts(checks)
    skipped = bool(checks) and all(c.get("answer") == "skip" for c in checks)
    colour = worst_colour(c for c, n in counts.items() if n)
    return {"colour": colour, "label": "Not used" if skipped else COLOUR_LABEL[colour],
            "counts": counts, "skipped": skipped, "total": len(checks)}


def counts_line(counts: Dict[str, int]) -> str:
    """"2 red · 3 yellow · 9 green · 4 not checked", leaving out zeros (all zero: "nothing to check")."""
    words = {RED: "red", YELLOW: "yellow", GREEN: "green", GREY: "not checked"}
    parts = [f"{counts[c]} {words[c]}" for c in (RED, YELLOW, GREEN, GREY) if counts.get(c)]
    return " · ".join(parts) or "nothing to check"


def summarize(checks: List[Check]) -> Dict[str, Any]:
    counts = {k: 0 for k in STATUS_ORDER}
    for c in checks:
        counts[c.status] += 1
    if counts[RISK]:
        verdict, tone = f"{counts[RISK]} thing{'s' if counts[RISK] != 1 else ''} to fix", RISK
    elif counts[WARN]:
        verdict, tone = f"{counts[WARN]} thing{'s' if counts[WARN] != 1 else ''} worth a look", WARN
    elif counts[TODO]:
        verdict, tone = f"{counts[TODO]} check{'s' if counts[TODO] != 1 else ''} left to do", TODO
    else:
        verdict, tone = "Looking good", OK
    colours = colour_counts(asdict(c) for c in checks)
    return {"counts": counts, "verdict": verdict, "tone": tone, "colour": colour_of(tone),
            "colours": colours, "counts_line": counts_line(colours)}


def build_review(sources: Iterable[Dict[str, Any]], mcp: Dict[str, Any], answers: Dict[str, Dict[str, Any]],
                 scan: Optional[Dict[str, Any]], now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    checks = safecontext_checks(sources, mcp, now_ms) + local_checks(scan) + guided_checks(answers, now_ms)
    for c in checks:
        c.group, c.colour, c.label = group_of(c.id), colour_of(c.status), label_of(c.status, c.answer)
    groups = []
    for gid, title, blurb in GROUPS:
        # Fixed order inside a group too (SafeContext's own facts, then the scan, then guided checks),
        # so a row stays put when its colour changes. The header carries the worst colour.
        rows = [asdict(c) for c in checks if c.group == gid]
        groups.append({"id": gid, "title": title, "blurb": blurb, "checks": rows, **rollup(rows)})
    summary = summarize(checks)
    attention = [g["title"] for g in groups if g["checks"] and g["colour"] in (RED, YELLOW)]
    return {"generated_ms": now_ms, "scan_on": scan is not None, **summary, "attention": attention,
            "groups": groups}


# --- the saved report -----------------------------------------------------------------------------
# Self-contained HTML (inline CSS, no scripts, nothing remote). It holds no account addresses,
# no MCP link and no config values, so it's safe to share.

_GLYPH = {RED: "✕", YELLOW: "!", GREEN: "✓", GREY: "?"}  # shape as well as colour


def _badge(colour: str, label: str) -> str:
    return (f"<span class='p {colour}'><span aria-hidden='true'>{_GLYPH[colour]}</span> "
            f"{html.escape(label)}</span>")


def report_html(review: Dict[str, Any], app_version: str = "") -> str:
    e = html.escape
    when = time.strftime("%-d %B %Y", time.localtime(review["generated_ms"] / 1000))
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        "<meta name='color-scheme' content='light dark'>",
        f"<title>AI access review · {e(when)}</title><style>",
        ":root{--t:#1d1d1f;--m:#666;--l:#ddd;--rb:#fde2e1;--r:#b3261e;--yb:#fff3cd;--y:#7a5600;--gb:#e3f4e8;"
        "--g:#1e7b3c;--nb:#ececec;--n:#555}"
        "@media (prefers-color-scheme:dark){:root{--t:#ececea;--m:#a09f9a;--l:#3a3a3e;--rb:#4a2420;--r:#ff8a7d;"
        "--yb:#3d3420;--y:#f2cf6b;--gb:#1f3a2a;--g:#6fd39a;--nb:#333336;--n:#bdbcb7}body{background:#1b1b1d}}"
        "body{font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;"
        "color:var(--t)}h1{font-size:26px;margin:0}h2{font-size:17px;margin:28px 0 2px;display:flex;gap:8px;"
        "align-items:center;flex-wrap:wrap}.gh{border-bottom:1px solid var(--l);padding-bottom:6px}"
        ".sub{color:var(--m)}.verdict{font-size:18px;font-weight:600;margin:14px 0 2px}"
        ".c{padding:10px 0;border-bottom:1px solid var(--l)}.t{font-weight:600}.p{display:inline-block;"
        "font-size:11px;font-weight:700;padding:1px 7px;border-radius:9px;margin-right:6px;vertical-align:1px}"
        ".red{background:var(--rb);color:var(--r)}.yellow{background:var(--yb);color:var(--y)}"
        ".green{background:var(--gb);color:var(--g)}.grey{background:var(--nb);color:var(--n)}"
        ".n{font-size:13px;font-weight:400;color:var(--m)}"
        ".d{color:var(--m);margin:3px 0}.f{margin:3px 0}ul{margin:4px 0 0 18px;padding:0;color:var(--m)}"
        "@media print{body{margin:0}}</style></head><body>",
        "<h1>AI access review</h1>",
        f"<div class='sub'>{e(when)} · made by SafeContext{(' ' + e(app_version)) if app_version else ''} "
        "on this Mac</div>",
        f"<div class='verdict'>{_badge(review['colour'], COLOUR_LABEL[review['colour']])}{e(review['verdict'])}</div>",
        f"<div class='sub'>{e(review['counts_line'])}</div>",
    ]
    if review.get("attention"):
        parts.append(f"<div class='sub'>Start with: {e(', '.join(review['attention']))}</div>")
    for g in review["groups"]:
        if not g["checks"]:
            continue
        parts.append(f"<div class='gh'><h2>{_badge(g['colour'], g['label'])}{e(g['title'])}"
                     f"<span class='n'>{e(counts_line(g['counts']))}</span></h2>"
                     f"<div class='sub'>{e(g['blurb'])}</div></div>")
        for c in g["checks"]:
            parts.append(f"<div class='c'><div class='t'>{_badge(c['colour'], c['label'])}{e(c['title'])}</div>")
            if c["detail"]:
                parts.append(f"<div class='d'>{e(c['detail'])}</div>")
            if c["items"]:
                parts.append("<ul>" + "".join(f"<li>{e(i)}</li>" for i in c["items"]) + "</ul>")
            if c["fix"]:
                parts.append(f"<div class='f'><b>What to do:</b> {e(c['fix'])}</div>")
            if c["link"].startswith("https://"):
                parts.append(f"<div class='f sub'>{e(c['link'])}</div>")
            parts.append("</div>")
    if not review["scan_on"]:
        parts.append("<p class='sub'>AI apps on this Mac weren't scanned. Turn on the scan in SafeContext to "
                     "include Claude Desktop and Claude Code.</p>")
    parts.append("<p class='sub'>What SafeContext can't see: grants stored by xAI or Anthropic and other apps' "
                 "macOS privacy permissions have no API, so those are checks you do yourself. Answers older than "
                 f"{REVIEW_EXPIRE_DAYS} days count as not checked.</p></body></html>")
    return "".join(parts)


def report_filename(now_ms: int) -> str:
    return time.strftime("ai-access-review-%Y-%m-%d.html", time.localtime(now_ms / 1000))

