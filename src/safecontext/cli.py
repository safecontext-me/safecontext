"""Command-line entry point (the Mac app will call the same functions).

    python -m safecontext connect [--headers-only] [--email you@gmail.com]   # Gmail
    python -m safecontext connect --source gcal [--email you@gmail.com]      # Google Calendar
    python -m safecontext sync [--source gcal]       # every connected source, or one
    python -m safecontext status
    python -m safecontext sources
    python -m safecontext search "rent transfer" [--source gmail] [--from @example.com] [--after 2025-01-01]
    python -m safecontext disconnect [--source gcal] [--wipe]
    python -m safecontext set-xai-key
    python -m safecontext ask "when is the dinner with Jamie?"
    python -m safecontext chat
    python -m safecontext mcp            # MCP server on stdio, for other AI clients
    python -m safecontext app [--demo]   # the Mac app window
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _store(args):
    from .store import DEFAULT_DATA_DIR, Store
    return Store(Path(args.data_dir or DEFAULT_DATA_DIR) / "mail.db")


def _print_progress(r) -> None:
    sys.stdout.write(f"\r{r.phase}: {r.fetched} fetched · {r.kept} kept · {r.dropped} dropped · {r.deleted} removed")
    sys.stdout.flush()


def _sources(store):
    from .sources import default_sources
    return default_sources(store)


def cmd_connect(args) -> int:
    from . import auth
    store = _store(args)
    src = _sources(store).get(args.source)
    opts = {"email": args.email}
    if args.source == "gmail":
        opts["mode"] = "headers" if args.headers_only else "full"
    try:
        account = src.connect(**opts)
    except auth.NotConfigured as e:
        print(e)
        return 2 if "already holds" not in str(e) else 1
    except auth.NeedsReconnect as e:
        print(e.message)
        return 1
    what = ""
    if args.source == "gmail":
        what = " (headers only)" if opts["mode"] == "headers" else " (full messages)"
    print(f"Connected {src.name}: {account}{what}. Run `sync` next.")
    return 0


def cmd_sync(args) -> int:
    store = _store(args)
    sources = _sources(store)
    targets = [sources.get(args.source)] if args.source else sources.connected()
    if not targets:
        print("Nothing is connected yet. Run `connect` first.")
        return 1
    ok = True
    for src in targets:  # one after another; a failing source doesn't stop the next
        print(f"{src.name}:")
        result = src.sync(progress=_print_progress)
        print()
        print(result.message or f"Done: {result.kept} kept, {result.dropped} dropped, {result.deleted} removed.")
        ok = ok and result.status == "ok"
    return 0 if ok else 1


def cmd_status(args) -> int:
    from .sync import read_heartbeat
    store = _store(args)
    g = store.state("gmail")
    info = {"account_email": g.get("account"), **{k: g.get(k) for k in ("sync_mode", "auth_state", "history_id",
                                                                       "backfill_page_token", "last_sync_ms")}}
    info["backfill_in_progress"] = bool(info.pop("backfill_page_token")) or info["history_id"] is None
    info.update(store.stats())
    info["sync_running"] = read_heartbeat(store)
    if info.get("auth_state") == "needs_reconnect":
        from .auth import RECONNECT_MESSAGE
        info["action"] = RECONNECT_MESSAGE
    info["sources"] = [{k: v for k, v in st.items() if k not in ("requirements", "description", "progress")}
                       for st in _sources(store).statuses() if st["available"]]
    print(json.dumps(info, indent=2))
    return 0


def cmd_sources(args) -> int:
    for st in _sources(_store(args)).statuses():
        line = f"{st['name']:<17} {st['state']:<16} {st['item_count']:>7} items"
        if st["account"]:
            line += f"  {st['account']}"
        print(line)
        if st["message"]:
            print(f"{'':<17} {st['message']}")
        for r in st["requirements"]:
            print(f"{'':<17} needs {r['label']}: {r['why']}")
    return 0


def cmd_search(args) -> int:
    from .tools import source_name
    store = _store(args)
    for h in store.search(args.query, source=args.source, sender=args.sender, after=args.after,
                          before=args.before, limit=args.limit):
        c = h.citation
        print(f"{c.date[:10]}  {source_name(h.source)}  {c.sender}\n  {h.title}\n  {h.snippet}\n  {c.link}\n")
    return 0


def cmd_disconnect(args) -> int:
    store = _store(args)
    sources = _sources(store)
    src = sources.get(args.source)
    sources.disconnect(args.source, wipe=args.wipe)
    if args.wipe:
        print(f"Disconnected {src.name} and deleted its data from this Mac.")
    else:
        print(f"Disconnected {src.name}. Its local scrubbed data was kept (use --wipe to delete it).")
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

    from . import migrate
    from .store import DEFAULT_DATA_DIR
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="safecontext")
    p.add_argument("--data-dir", help="defaults to ~/Library/Application Support/SafeContext")
    sub = p.add_subparsers(dest="cmd")
    source_ids = ["gmail", "gcal"]
    c = sub.add_parser("connect")
    c.add_argument("--source", choices=source_ids, default="gmail")
    c.add_argument("--headers-only", action="store_true", help="Gmail: use gmail.metadata, no message bodies")
    c.add_argument("--email", help="pre-fill the Google account chooser")
    sy = sub.add_parser("sync")
    sy.add_argument("--source", choices=source_ids, help="default: every connected source")
    sub.add_parser("status")
    sub.add_parser("sources")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--source", choices=source_ids)
    s.add_argument("--from", dest="sender")
    s.add_argument("--after")
    s.add_argument("--before")
    s.add_argument("--limit", type=int, default=10)
    d = sub.add_parser("disconnect")
    d.add_argument("--source", choices=source_ids, default="gmail")
    d.add_argument("--wipe", action="store_true")
    sub.add_parser("set-xai-key")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--model", help="xAI model id (default grok-4.7, or SAFECONTEXT_GROK_MODEL)")
    ch = sub.add_parser("chat")
    ch.add_argument("--model")
    sub.add_parser("mcp")
    ap = sub.add_parser("app")
    ap.add_argument("--demo", action="store_true", help="synthetic mailbox, no Google or xAI needed")
    ap.add_argument("--debug", action="store_true", help="enable the web inspector")
    args = p.parse_args(argv)
    if args.cmd is None:  # double-clicking the packaged .app runs with no arguments
        args = p.parse_args(([] if argv is None else list(argv)) + ["app"])
    try:
        migrate.migrate_data_dir()
    except OSError as e:
        # Don't carry on with an empty mailbox while the real one sits under the old name.
        print(f"Couldn't move your data from {migrate.LEGACY_DATA_DIR} to {DEFAULT_DATA_DIR}: {e}",
              file=sys.stderr)
        return 1
    return {"connect": cmd_connect, "sync": cmd_sync, "status": cmd_status, "sources": cmd_sources, "search": cmd_search,
            "disconnect": cmd_disconnect, "set-xai-key": cmd_set_xai_key, "ask": cmd_ask, "chat": cmd_chat,
            "mcp": cmd_mcp, "app": cmd_app}[args.cmd](args)


def legacy_main(argv=None) -> int:
    """The old `mycontext` command and `python -m mycontext`. Deprecated; kept so MCP client
    configs written before the rename keep working. The notice goes to stderr because
    stdout carries MCP's JSON-RPC."""
    print("mycontext is now SafeContext. Use `safecontext` (or `python -m safecontext`); "
          "the mycontext name will be removed in a future version.", file=sys.stderr)
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())
