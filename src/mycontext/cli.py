"""Command-line entry point (the Mac app will call the same functions).

    python -m mycontext connect [--headers-only] [--email you@gmail.com]
    python -m mycontext sync
    python -m mycontext status
    python -m mycontext search "rent transfer" [--from @example.com] [--after 2025-01-01]
    python -m mycontext disconnect [--wipe]
    python -m mycontext set-xai-key
    python -m mycontext ask "when is the dinner with Jamie?"
    python -m mycontext chat
    python -m mycontext mcp            # MCP server on stdio, for other AI clients
    python -m mycontext app [--demo]   # the Mac app window
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)  # google-auth nags about Python 3.9


def _store(args):
    from .store import DEFAULT_DATA_DIR, Store
    return Store(Path(args.data_dir or DEFAULT_DATA_DIR) / "mail.db")


def _print_progress(r) -> None:
    sys.stdout.write(f"\r{r.phase}: {r.fetched} fetched · {r.kept} kept · {r.dropped} dropped · {r.deleted} removed")
    sys.stdout.flush()


def cmd_connect(args) -> int:
    from . import auth
    mode = "headers" if args.headers_only else "full"
    store = _store(args)
    try:
        _, account = auth.connect(mode, login_hint=args.email)
    except auth.NotConfigured as e:
        print(e)
        return 2
    except auth.NeedsReconnect as e:
        print(e.message)
        return 1
    previous = store.get_meta("account_email")
    if previous and previous != account:
        print(f"This data folder belongs to {previous}. Run `disconnect --wipe` first to switch accounts.")
        auth.TokenStore().delete(account)
        return 1
    store.set_meta("account_email", account)
    store.set_meta("sync_mode", mode)
    store.set_meta("auth_state", "ok")
    print(f"Connected {account} ({'headers only' if mode == 'headers' else 'full messages'}). Run `sync` next.")
    return 0


def cmd_sync(args) -> int:
    from . import auth
    from .gmail_api import GoogleGmailAPI
    from .sync import sync_account

    store = _store(args)
    account = store.get_meta("account_email") or ""
    mode = store.get_meta("sync_mode") or "full"
    try:
        creds = auth.get_credentials(account, mode)
    except auth.NeedsReconnect as e:
        store.set_meta("auth_state", "needs_reconnect")
        print(e.message)
        return 1
    except auth.TransientAuthError:
        print("Couldn't reach Google. Check your connection and try again.")
        return 1
    except auth.NotConfigured as e:
        print(e)
        return 1
    result = sync_account(store, GoogleGmailAPI(creds), mode, progress=_print_progress)
    print()
    print(result.message or f"Done: {result.kept} kept, {result.dropped} dropped, {result.deleted} removed.")
    return 0 if result.status == "ok" else 1


def cmd_status(args) -> int:
    store = _store(args)
    info = {k: store.get_meta(k) for k in ("account_email", "sync_mode", "auth_state", "history_id",
                                           "backfill_page_token", "last_sync_ms")}
    info["backfill_in_progress"] = bool(info.pop("backfill_page_token")) or info["history_id"] is None
    info.update(store.stats())
    from .sync import read_heartbeat
    info["sync_running"] = read_heartbeat(store)
    if info.get("auth_state") == "needs_reconnect":
        from .auth import RECONNECT_MESSAGE
        info["action"] = RECONNECT_MESSAGE
    print(json.dumps(info, indent=2))
    return 0


def cmd_search(args) -> int:
    store = _store(args)
    for h in store.search(args.query, sender=args.sender, after=args.after, before=args.before, limit=args.limit):
        c = h.citation
        print(f"{c.date[:10]}  {c.sender}\n  {h.subject}\n  {h.snippet}\n  {c.link}\n")
    return 0


def cmd_disconnect(args) -> int:
    from . import auth
    store = _store(args)
    account = store.get_meta("account_email") or ""
    if account:
        auth.disconnect(account)
    if args.wipe:
        store.wipe()
        print("Disconnected and deleted all local mail.")
    else:
        store.set_meta("auth_state", "needs_reconnect")
        print("Disconnected. Local scrubbed mail was kept (use --wipe to delete it).")
    return 0


def _print_answer(ans) -> None:
    print(ans.text)


def cmd_set_xai_key(args) -> int:
    import getpass
    from .grok import set_api_key
    key = getpass.getpass("xAI API key (input hidden): ")
    if not key.strip():
        print("No key entered.")
        return 1
    set_api_key(key)
    print("Saved to the macOS Keychain.")
    return 0


def _agent(args):
    from .grok import GrokMailAgent
    return GrokMailAgent(_store(args), model=args.model)


def cmd_ask(args) -> int:
    from .grok import GrokError
    try:
        agent = _agent(args)
        _print_answer(agent.ask(args.question))
    except GrokError as e:
        print(e)
        return 1
    return 0


def cmd_chat(args) -> int:
    from .grok import GrokError
    try:
        agent = _agent(args)
    except GrokError as e:
        print(e)
        return 1
    print("Ask about your mail. Empty line or Ctrl-D to quit.")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not q:
            return 0
        try:
            _print_answer(agent.ask(q))
        except GrokError as e:
            print(e)


def cmd_mcp(args) -> int:
    from .mcp_server import main as serve
    serve(_store(args))
    return 0


def cmd_app(args) -> int:
    from .app.main import run
    run(demo=args.demo, data_dir=Path(args.data_dir) if args.data_dir else None, debug=args.debug)
    return 0


def main(argv=None) -> int:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="mycontext")
    p.add_argument("--data-dir", help="defaults to ~/Library/Application Support/MyContext")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("connect")
    c.add_argument("--headers-only", action="store_true", help="use gmail.metadata: no message bodies")
    c.add_argument("--email", help="pre-fill the Google account chooser")
    sub.add_parser("sync")
    sub.add_parser("status")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--from", dest="sender")
    s.add_argument("--after")
    s.add_argument("--before")
    s.add_argument("--limit", type=int, default=10)
    d = sub.add_parser("disconnect")
    d.add_argument("--wipe", action="store_true")
    sub.add_parser("set-xai-key")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--model", help="xAI model id (default grok-4.7, or MYCONTEXT_GROK_MODEL)")
    ch = sub.add_parser("chat")
    ch.add_argument("--model")
    sub.add_parser("mcp")
    ap = sub.add_parser("app")
    ap.add_argument("--demo", action="store_true", help="synthetic mailbox, no Google or xAI needed")
    ap.add_argument("--debug", action="store_true", help="enable the web inspector")
    args = p.parse_args(argv)
    if args.cmd is None:  # double-clicking the packaged .app runs with no arguments
        args = p.parse_args(([] if argv is None else list(argv)) + ["app"])
    return {"connect": cmd_connect, "sync": cmd_sync, "status": cmd_status, "search": cmd_search,
            "disconnect": cmd_disconnect, "set-xai-key": cmd_set_xai_key, "ask": cmd_ask, "chat": cmd_chat,
            "mcp": cmd_mcp, "app": cmd_app}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
