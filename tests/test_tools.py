import pytest

from safecontext.scrub import scrub
from safecontext.store import Store
from safecontext.tools import MAX_BODY_CHARS, MAX_THREAD_MESSAGES, TOOLS, citations, run_tool
from safecontext.demo.synthetic import mailbox, msg


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mail.db", account="alex@example.org")
    s.put_many(scrub(m) for m in mailbox())
    s.set_state("gmail", "history_id", "1")
    yield s
    s.close()


def test_schemas_are_objects_with_descriptions():
    for t in TOOLS:
        assert t["parameters"]["type"] == "object" and t["description"]
        assert t["parameters"]["additionalProperties"] is False


def test_search_results_carry_citations(store):
    r = run_tool(store, "search_mail", {"query": "rent"})
    (hit,) = r["results"]
    assert hit["date"] == "2025-11-03T18:03:00Z"
    assert hit["sender"] == "Morgan Lee <morgan@example.net>"
    assert hit["gmail_link"].startswith("https://mail.google.com/mail/?authuser=alex%40example.org#search/")
    assert "notice" not in r


def test_search_filters(store):
    assert run_tool(store, "search_mail", {"query": "rent", "sender": "@example.com"})["count"] == 0
    assert run_tool(store, "search_mail", {"query": "rent", "after": "2026-01-01"})["count"] == 0


def test_read_thread(store):
    r = run_tool(store, "read_thread", {"thread_id": "t0005"})
    (m,) = r["messages"]
    assert "[REDACTED:ONE_TIME_CODE]" in m["body"] and "482913" not in m["body"]
    assert m["redactions"] == {"ONE_TIME_CODE": 1, "SSN": 1}
    assert [a["filename"] for a in m["attachments"]][1] == "boarding-pass.png"
    assert m["gmail_link"] and m["sender"] and m["date"]


def test_read_thread_unknown(store):
    r = run_tool(store, "read_thread", {"thread_id": "nope"})
    assert r["messages"] == [] and "note" in r


def test_read_thread_truncates(store):
    for i in range(MAX_THREAD_MESSAGES + 5):
        m = scrub(msg(200 + i, text="x" * (MAX_BODY_CHARS + 100)))
        m.thread_id = "big"
        store.put(m)
    r = run_tool(store, "read_thread", {"thread_id": "big"})
    assert len(r["messages"]) == MAX_THREAD_MESSAGES and r["older_messages_omitted"] == 5
    assert all(len(m["body"]) <= MAX_BODY_CHARS for m in r["messages"])
    assert sum(len(m["body"]) for m in r["messages"]) <= 40000
    assert r["messages"][-1]["body_truncated"] is True
    assert r["messages"][0]["date"] < r["messages"][-1]["date"]


def test_list_messages(store):
    r = run_tool(store, "list_messages", {"sender": "jamie", "limit": 500})
    assert [x["id"] for x in r["results"]] == ["m0005", "m0001"]


@pytest.mark.parametrize("name,args,fragment", [
    ("search_mail", {}, "'query' is required"),
    ("search_mail", {"query": 5}, "must be a string"),
    ("search_mail", {"query": "x", "limit": "lots"}, "integer"),
    ("search_mail", {"query": "x", "after": "last tuesday"}, "invalid argument"),
    ("search_mail", {"query": "x", "drop_table": 1}, "unknown argument"),
    ("delete_mail", {}, "unknown tool"),
])
def test_bad_calls_return_errors_not_exceptions(store, name, args, fragment):
    assert fragment in run_tool(store, name, args)["error"]


def test_reconnect_notice(store):
    store.set_state("gmail", "auth_state", "needs_reconnect")
    r = run_tool(store, "search_mail", {"query": "rent"})
    assert "Reconnect Gmail" in r["notice"] and r["count"] == 1  # still serves local mail


def test_syncing_notice(store):
    store.set_state("gmail", "history_id", None)
    store.set_state("gmail", "account", "alex@example.org")
    assert "still running" in run_tool(store, "list_messages", {})["notice"]


def test_citations_helper(store):
    r = run_tool(store, "read_thread", {"thread_id": "t0001"})
    (c,) = citations(r)
    assert {"date", "sender", "subject", "gmail_link", "link", "source"} <= set(c)
    assert c["source"] == "gmail" and c["link"] == c["gmail_link"]
