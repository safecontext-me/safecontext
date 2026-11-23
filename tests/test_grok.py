"""Grok agent loop against a fake xAI client. Also checks exactly what
would leave the Mac: no planted secret may appear in any request."""
import json
from types import SimpleNamespace

import pytest

from safecontext import grok
from safecontext.grok import GrokError, GrokMailAgent, grok_tools
from safecontext.scrub import scrub
from safecontext.store import Store
from safecontext.demo.synthetic import all_needles, leak_matrix, mailbox


class Item(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


def call(name, args, cid):
    return Item(type="function_call", name=name, arguments=json.dumps(args), call_id=cid, id=f"fc_{cid}")


def message(text):
    return Item(type="message", role="assistant", content=[{"type": "output_text", "text": text}])


class FakeXAI:
    """Replays scripted turns and records every request payload."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        out = self.turns.pop(0)
        if isinstance(out, Exception):
            raise out
        text = " ".join(c["text"] for o in out if o.type == "message" for c in o.content)
        return SimpleNamespace(output=out, output_text=text)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mail.db", account="alex@example.org")
    s.put_many(scrub(m) for m in mailbox() + leak_matrix())
    s.set_state("gmail", "history_id", "1")
    yield s
    s.close()


def test_tool_loop_and_guaranteed_sources(store):
    fake = FakeXAI([
        [call("search_mail", {"query": "rent transfer"}, "c1")],
        [call("read_thread", {"thread_id": "t0003"}, "c2")],
        [message("Morgan sent bank details for the rent; the numbers were redacted.")],
    ])
    ans = GrokMailAgent(store, client=fake).ask("How do I pay Morgan rent?")
    assert ans.tool_calls == 2
    assert "Sources:" in ans.text and "rfc822msgid%3Asynthetic-3%40example.com" in ans.text
    assert "2025-11-03 · Morgan Lee <morgan@example.net> · Rent split" in ans.text
    assert ans.sources[0]["gmail_link"].startswith("https://mail.google.com/")


def test_no_footer_when_model_already_cites(store):
    fake = FakeXAI([[call("search_mail", {"query": "rent"}, "c1")],
                    [message("See https://mail.google.com/mail/?authuser=x#search/rfc822msgid%3Asynthetic-3")]])
    assert "Sources:" not in GrokMailAgent(store, client=fake).ask("rent?").text


def test_request_shape(store):
    fake = FakeXAI([[message("hi")]])
    GrokMailAgent(store, client=fake, model="grok-test").ask("hello")
    req = fake.requests[0]
    assert req["model"] == "grok-test" and req["store"] is False and req["tool_choice"] == "auto"
    assert req["tools"] == grok_tools()
    assert all(t["type"] == "function" and "parameters" in t and "function" not in t for t in req["tools"])
    assert req["input"][0]["role"] == "system" and "REDACTED" in req["input"][0]["content"]


def test_model_from_env(store, monkeypatch):
    monkeypatch.setenv(grok.MODEL_ENV, "grok-env")
    fake = FakeXAI([[message("ok")]])
    GrokMailAgent(store, client=fake).ask("x")
    assert fake.requests[0]["model"] == "grok-env"


def test_default_model():
    assert grok.DEFAULT_MODEL == "grok-4.7"


def test_parallel_calls_each_get_output(store):
    fake = FakeXAI([[call("search_mail", {"query": "rent"}, "a"), call("list_messages", {"sender": "jamie"}, "b")],
                    [message("done")]])
    GrokMailAgent(store, client=fake).ask("x")
    outputs = [i for i in fake.requests[1]["input"] if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["a", "b"]


def test_bad_tool_arguments_go_back_to_model(store):
    fake = FakeXAI([[Item(type="function_call", name="search_mail", arguments="{not json", call_id="c1")],
                    [call("delete_everything", {}, "c2")],
                    [message("sorry")]])
    GrokMailAgent(store, client=fake).ask("x")
    outs = [json.loads(i["output"]) for i in fake.requests[2]["input"] if i.get("type") == "function_call_output"]
    assert "not valid JSON" in outs[0]["error"] and "unknown tool" in outs[1]["error"]


def test_reasoning_items_not_replayed(store):
    fake = FakeXAI([[Item(type="reasoning", id="r1", summary=[]), call("search_mail", {"query": "rent"}, "c1")],
                    [message("ok")]])
    GrokMailAgent(store, client=fake).ask("x")
    assert not any(i.get("type") == "reasoning" for i in fake.requests[1]["input"])


def test_round_limit(store):
    fake = FakeXAI([[call("search_mail", {"query": "rent"}, f"c{i}")] for i in range(3)])
    ans = GrokMailAgent(store, client=fake, max_rounds=3).ask("loop forever")
    assert "step limit" in ans.text and "Sources:" in ans.text


def test_conversation_continues(store):
    fake = FakeXAI([[message("first")], [message("second")]])
    agent = GrokMailAgent(store, client=fake)
    agent.ask("one")
    agent.ask("two")
    users = [i["content"] for i in fake.requests[1]["input"] if i.get("role") == "user"]
    assert users == ["one", "two"]


def test_failed_turn_rolls_back_history(store):
    class AuthenticationError(Exception):
        pass

    fake = FakeXAI([AuthenticationError("401")])
    agent = GrokMailAgent(store, client=fake)
    with pytest.raises(GrokError, match="API key"):
        agent.ask("x")
    assert len(agent.history) == 1


def test_reconnect_notice_reaches_model(store):
    store.set_state("gmail", "auth_state", "needs_reconnect")
    fake = FakeXAI([[call("search_mail", {"query": "rent"}, "c1")], [message("ok")]])
    GrokMailAgent(store, client=fake).ask("x")
    out = next(i for i in fake.requests[1]["input"] if i.get("type") == "function_call_output")
    assert "Reconnect Gmail" in json.loads(out["output"])["notice"]


def test_no_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv(grok.KEY_ENV, raising=False)
    monkeypatch.setattr(grok.keyring, "get_password", lambda *a: None)
    with pytest.raises(GrokError, match="set-xai-key"):
        grok.make_client()


def test_client_points_at_xai(monkeypatch):
    c = grok.make_client(api_key="xai-test")
    assert str(c.base_url).rstrip("/") == "https://api.x.ai/v1"


def test_nothing_secret_leaves_the_mac(store):
    """Grok reads every thread in the leak matrix; no planted secret may be in any request."""
    threads = sorted({r[0] for r in store._db.execute("SELECT thread_id FROM items")})
    turns = [[call("search_mail", {"query": q}, f"s{i}")] for i, q in
             enumerate(["card", "password", "code", "key", "IBAN", "address", "SSN", "phone", "routing"])]
    turns += [[call("read_thread", {"thread_id": t}, f"t{i}") for i, t in enumerate(threads)]]
    turns += [[call("list_messages", {"limit": 100}, "l1")], [message("summary")]]
    fake = FakeXAI(turns)
    GrokMailAgent(store, client=fake, max_rounds=20).ask("show me everything sensitive")
    payload = json.dumps(fake.requests, ensure_ascii=False)
    assert "BENIGN-MARKER" in payload  # mail really was sent
    leaks = [n for n in all_needles() if n in payload]
    assert not leaks, leaks


def test_cli_ask_without_key_prints_message(monkeypatch, tmp_path, capsys):
    from keyring.backends.fail import Keyring as FailKeyring
    from safecontext.cli import main
    monkeypatch.delenv(grok.KEY_ENV, raising=False)
    old = grok.keyring.get_keyring()
    grok.keyring.set_keyring(FailKeyring())
    try:
        assert main(["--data-dir", str(tmp_path), "ask", "hello"]) == 1
    finally:
        grok.keyring.set_keyring(old)
    assert "set-xai-key" in capsys.readouterr().out
