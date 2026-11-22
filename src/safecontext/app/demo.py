"""Demo wiring: a fake Gmail and a local stand-in for Grok, so the app can be
tried without a Google client, a Gmail account, or an xAI key.
Nothing in demo mode touches the network."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from ..demo.fake_gmail import FakeGmail
from ..demo.synthetic import mailbox, msg

DEMO_ACCOUNT = "alex@example.org"


def _extra_mail():
    return [
        msg(30, frm="Jamie Rivera <jamie@example.com>", subject="Re: Dinner Saturday?",
            text="Perfect, 7pm works. I booked Nopa for 4 people under Rivera.\n\nJamie"),
        msg(31, frm="Taylor Wu <taylor@example.org>", subject="Q1 planning doc",
            text="Draft is ready for review. Main open question is whether we hire two engineers in Q1 or wait "
                 "for the budget review on Jan 15."),
        msg(32, frm="Sam Patel <sam@example.dev>", subject="Re: staging creds",
            text="Rotated everything. New key is sk-proj-" + "Demo0123456789" * 3 + " — please don't paste it "
                 "in Slack this time."),
    ]


class SlowFakeGmail(FakeGmail):
    """Adds a small delay per batch so the progress display is visible."""

    def get_messages(self, ids, fmt):
        time.sleep(0.4)
        return super().get_messages(ids, fmt)


def make_fake_gmail() -> FakeGmail:
    g = SlowFakeGmail(page_size=5, email=DEMO_ACCOUNT)
    for m in mailbox() + _extra_mail():
        g.add(m, record=False)
    return g


class _Item(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


class DemoGrok:
    """Stands in for the xAI client: searches with the question's words, then
    answers by listing what it found. Nothing is sent anywhere."""

    def __init__(self) -> None:
        self.responses = SimpleNamespace(create=self._create)

    _STOP = {"what", "when", "where", "who", "which", "does", "have", "this", "that", "with", "from", "about",
             "tell", "info", "recent", "recently", "mail", "email", "emails", "sent", "send", "anything", "there",
             "their", "they", "them", "were", "would", "could", "should", "summarize", "show", "find", "asked"}

    def _keywords(self, question: str) -> List[str]:
        words = [w.strip("?,.!\"'“”").lower() for w in question.split()]
        words = [w for w in words if len(w) > 3 and w not in self._STOP]
        return sorted(dict.fromkeys(words), key=len, reverse=True)[:3] or [question]

    def _create(self, *, input: List[Dict[str, Any]], **_: Any):
        last_user = max(n for n, i in enumerate(input) if i.get("role") == "user")
        question = input[last_user]["content"]
        answered = [i for i in input[last_user:] if i.get("type") == "function_call_output"]
        keywords = self._keywords(question)
        found = json.loads(answered[-1]["output"]).get("results", []) if answered else []
        if not found and len(answered) < len(keywords):  # try the next keyword
            call = _Item(type="function_call", name="search_mail", call_id=f"demo{len(answered)}",
                         arguments=json.dumps({"query": keywords[len(answered)]}))
            return SimpleNamespace(output=[call], output_text="")
        if found:
            lines = [f"• {r['subject']} — {r['sender']}, {r['date'][:10]}" for r in found[:5]]
            text = ("Demo mode: this answer is generated locally, not by Grok. Matching messages:\n"
                    + "\n".join(lines))
        else:
            text = ("Demo mode: this answer is generated locally, not by Grok. I didn't find matching mail; "
                    "try words like “dinner”, “rent” or “planning”.")
        return SimpleNamespace(output=[_Item(type="message", role="assistant",
                                             content=[{"type": "output_text", "text": text}])], output_text=text)


def demo_backend_kwargs(data_dir: Optional[Path] = None) -> Dict[str, Any]:
    gmail = make_fake_gmail()
    return dict(
        data_dir=data_dir or Path(tempfile.mkdtemp(prefix="safecontext-demo-")),
        demo=True,
        gmail_factory=lambda account, mode: gmail,
        connect_fn=lambda mode, email: (time.sleep(0.8), DEMO_ACCOUNT)[1],
        grok_client_factory=DemoGrok,
    )
