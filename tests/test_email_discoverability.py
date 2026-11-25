"""Eval: SafeContext is connected and there is no Gmail connector.

Prompts about the user's mail must select SafeContext tools. The selector is
lexical on purpose: it only picks a tool whose description says it searches
the user's Gmail inbox. The old generic descriptions ("every connected source
(Gmail, Google Calendar, ...)") do not, so this fails closed on that copy.
It does not call a model, and it does not claim anything about prompt injection.
"""
import pytest

from safecontext.demo.synthetic import mailbox
from safecontext.grok_link import MCPHttpHandler
from safecontext.scrub import scrub
from safecontext.store import Store
from safecontext.tools import grok_tools, tools_for

PROMPTS = (
    "check my email",
    "any reply from Coast?",
    "do you have access to my email?",
)
_EMAIL_WORDS = ("email", "inbox", "reply", "mail")
_ANSWER_TOOLS = {"search", "search_email", "read", "read_email_thread"}
# A Gmail connector would advertise tools like these. This eval has none.
_GMAIL_CONNECTOR_TOOLS = {"gmail_search", "gmail_read", "get_email", "send_email", "create_draft"}
_OLD_DESCRIPTION = (
    "Full-text search across the user's scrubbed data from every connected source "
    "(Gmail, Google Calendar, ...). Sensitive data has been replaced with [REDACTED:TYPE]."
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mail.db", account="alex@example.org")
    s.put_many(scrub(m) for m in mailbox())
    s.set_state("gmail", "account", "alex@example.org")
    s.set_state("gmail", "history_id", "1")
    yield s
    s.close()


def _email_question(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(word in lowered for word in _EMAIL_WORDS)


def _covers_user_email(description: str) -> bool:
    """True when the description names the user's Gmail and their inbox, not Gmail as an example."""
    lowered = description.lower()
    names_mailbox = "user's gmail" in lowered or "user's email" in lowered
    talks_about_mail = "inbox" in lowered and "email" in lowered
    return names_mailbox and talks_about_mail


def select_tools(prompt: str, catalog: list) -> list:
    """Tools a client would call for this prompt. Empty when nothing in the catalog covers email."""
    if not _email_question(prompt):
        return []
    return [tool["name"] for tool in catalog if _covers_user_email(tool["description"])]


@pytest.mark.parametrize("catalog_for", [tools_for, grok_tools])
@pytest.mark.parametrize("prompt", PROMPTS)
def test_email_prompts_select_safecontext_tools(store, catalog_for, prompt):
    catalog = catalog_for(store)
    names = {tool["name"] for tool in catalog}
    assert names.isdisjoint(_GMAIL_CONNECTOR_TOOLS)
    assert "search_email" in names  # Gmail is connected inside SafeContext; there is no other connector

    chosen = select_tools(prompt, catalog)
    assert chosen, prompt
    assert set(chosen) <= names
    assert set(chosen) & _ANSWER_TOOLS

    # The previous descriptions mention Gmail only as an example, so the same prompts select nothing.
    stale = [{**tool, "description": _OLD_DESCRIPTION} for tool in catalog]
    assert select_tools(prompt, stale) == []


def test_grok_list_matches_the_eval_catalog(store):
    """The path that used to hide aliases: Grok's tools/list is the eval catalog."""
    import json
    handler = MCPHttpHandler(store)
    body = handler.handle("POST", {}, json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))[2]
    listed = {tool["name"] for tool in json.loads(body)["result"]["tools"]}
    assert listed == {tool["name"] for tool in grok_tools(store)}
    assert listed.isdisjoint(_GMAIL_CONNECTOR_TOOLS)
