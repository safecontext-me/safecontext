"""Demo wiring: a fake Gmail, a fake Google Calendar, a synthetic Messages database and a stand-in
for the Grok relay, so the app can be tried without a Google client, a Gmail account or Full Disk Access.
Nothing in demo mode touches the network."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..demo.fake_ai_configs import demo_ai_home
from ..demo.fake_calendar import make_fake_calendar
from ..demo.fake_dropbox import make_fake_dropbox
from ..demo.fake_files import demo_files
from ..demo.fake_gmail import FakeGmail
from ..demo.fake_messages import demo_imessage_options
from ..demo.fake_update import demo_updater
from ..demo.synthetic import demo_mailbox, mailbox, msg
from ..grok_link import demo_link
from ..permlog import PermissionLog

DEMO_ACCOUNT = "alex@example.org"

# A made-up permission history: Grok asking again and again over the past week, as it does when it
# keeps losing its sign-in, plus a few setting changes.
_DEMO_HISTORY = [  # (days ago, kind, outcome, client, note)
    (47.0, "link", "on", "", ""), (47.0, "signin", "created", "", ""),
    (9.0, "connect", "approved", "Grok", ""), (9.0, "signin", "created", "Grok", ""),
    (8.9, "used", "", "Grok", ""),
    (6.2, "connect", "approved", "Claude", ""), (6.2, "signin", "created", "Claude", ""),
    (5.1, "connect", "denied", "Grok", ""), (4.0, "connect", "expired", "Grok", ""),
    (3.4, "signin", "expired", "Grok", ""),
    (3.3, "link_only", "on", "", ""), (3.2, "link_only", "off", "", ""),
    (2.5, "connect", "approved", "Grok", ""), (2.5, "signin", "created", "Grok", ""),
    (2.0, "signin", "revoked", "Grok", ""),
    (1.4, "connect", "denied", "Grok", ""), (1.0, "used", "", "Claude", ""),
    (0.8, "connect", "cancelled", "Grok", ""),
]


def _demo_link_with_history(store, data_dir):
    path = Path(data_dir) / "permission_log.json"
    if not path.exists():
        now = int(time.time() * 1000)
        for days, kind, outcome, client, note in _DEMO_HISTORY:
            PermissionLog(path, now=lambda d=days: now - int(d * 86_400_000)).record(kind, outcome, client, note)
    return demo_link(store, data_dir)


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


# Demo Gmail labels (ids as Gmail makes them, colours from Gmail's palette), spread over the demo mail.
_DEMO_LABELS = {"Label_1": {"name": "Receipts", "color": "#16a766"}, "Label_2": {"name": "Work", "color": "#4a86e8"},
                "Label_3": {"name": "Work/Clients", "color": "#4a86e8"}, "Label_4": {"name": "Family", "color": "#f691b2"},
                "Label_5": {"name": "Travel", "color": "#ffad47"}}
_DEMO_LABEL_EVERY = (("Label_2", 5), ("Label_1", 7), ("STARRED", 9), ("Label_4", 11), ("Label_5", 13), ("Label_3", 17))


def make_fake_gmail() -> FakeGmail:
    g = SlowFakeGmail(page_size=25, email=DEMO_ACCOUNT)
    g.label_defs = dict(_DEMO_LABELS)
    for n, m in enumerate(mailbox() + _extra_mail() + demo_mailbox()):
        m.label_ids = list(m.label_ids) + [lid for lid, every in _DEMO_LABEL_EVERY if n % every == 3]
        g.add(m, record=False)
    return g


def demo_backend_kwargs(data_dir: Optional[Path] = None) -> Dict[str, Any]:
    gmail = make_fake_gmail()
    calendar = make_fake_calendar(DEMO_ACCOUNT)
    dropbox = make_fake_dropbox(DEMO_ACCOUNT)
    data_dir = data_dir or Path(tempfile.mkdtemp(prefix="safecontext-demo-"))
    # Files: made-up folders and PDFs beside the demo data. The demo starts with none added (the empty
    # state); Add folder / Add PDF add the made-up ones in turn instead of opening the real Finder panel.
    # To start with them added, pass pdf_start=_pdf_start below.
    _pdf_start, pdf_chooser = demo_files(Path(data_dir) / "demo-files")
    return dict(
        data_dir=data_dir,
        demo=True,
        gmail_factory=lambda account, mode: gmail,
        connect_fn=lambda mode, email: (time.sleep(0.8), DEMO_ACCOUNT)[1],
        calendar_factory=lambda account: calendar,
        calendar_connect_fn=lambda email: (time.sleep(0.8), DEMO_ACCOUNT)[1],
        grok_link=_demo_link_with_history,
        # A synthetic chat.db beside the demo data; Full Disk Access starts "off" and the
        # Open System Settings button turns it on. Never the real ~/Library/Messages.
        imessage_options=demo_imessage_options(Path(data_dir) / "demo-messages"),
        # A made-up Dropbox: Connect never opens the real Dropbox sign-in, and needs no app key.
        source_options={"dropbox": {"api_factory": lambda account: dropbox,
                                    "connect_fn": lambda email: (time.sleep(0.8), DEMO_ACCOUNT)[1],
                                    "revoke_fn": lambda account, revoke=True: None}},
        # The AI access review's opt-in scan reads a made-up home, never the real one.
        review_home=demo_ai_home(Path(data_dir) / "demo-home"),
        # Check for updates finds a made-up next version; installing goes through the real updater
        # against fakes and never replaces, spawns or opens anything (demo/fake_update.py).
        updater=demo_updater(Path(data_dir)),
        pdf_chooser=pdf_chooser,
    )
