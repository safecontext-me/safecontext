"""Demo wiring: a fake Gmail, a fake Google Calendar and a stand-in for the Grok relay, so the app
can be tried without a Google client or a Gmail account.
Nothing in demo mode touches the network."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..demo.fake_calendar import make_fake_calendar
from ..demo.fake_gmail import FakeGmail
from ..demo.synthetic import mailbox, msg
from ..grok_link import demo_link

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


def demo_backend_kwargs(data_dir: Optional[Path] = None) -> Dict[str, Any]:
    gmail = make_fake_gmail()
    calendar = make_fake_calendar(DEMO_ACCOUNT)
    return dict(
        data_dir=data_dir or Path(tempfile.mkdtemp(prefix="safecontext-demo-")),
        demo=True,
        gmail_factory=lambda account, mode: gmail,
        connect_fn=lambda mode, email: (time.sleep(0.8), DEMO_ACCOUNT)[1],
        calendar_factory=lambda account: calendar,
        calendar_connect_fn=lambda email: (time.sleep(0.8), DEMO_ACCOUNT)[1],
        grok_link=demo_link,
    )
