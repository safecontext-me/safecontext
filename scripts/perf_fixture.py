"""Dev only: build a large synthetic store for timing the Mail view. Never touches real data.

    .venv/bin/python scripts/perf_fixture.py /tmp/some-dir [--gmail 128000] [--imessage 40000] [--gcal 9000]

Writes <dir>/mail.db shaped like a big real mailbox: Gmail threads of 1-6 messages with some
dropped mail, iMessage as a few hundred long chats, and Google Calendar with weekly series that
run into 2031. Every word is made up. Then browse it with scripts/dev_preview.py --data-dir <dir>.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent / "src")]

from safecontext.scrub import SCRUBBER_VERSION  # noqa: E402
from safecontext.store import Store  # noqa: E402

WORDS = ("project budget lunch meeting invoice travel review draft notes plan family weekend call update "
         "photo dinner flight hotel school soccer doctor garden coffee report deadline launch").split()
DAY = 86_400_000
NOW = int(time.time() * 1000)


def _text(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def _item(source, i, kind, thread, date, sender, title, body, end=None, red="{}", account="alex@example.org"):
    return (source, f"{source}-{i}", account, kind, thread, date, end, sender, sender.lower(), "", title, body,
            "[]", "", "", "{}", red, SCRUBBER_VERSION)


def build(data_dir: Path, gmail: int, imessage: int, gcal: int, seed: int = 7,
          body_words: tuple = (40, 600)) -> Path:
    rng = random.Random(seed)
    store = Store(data_dir / "mail.db")
    items, dropped = [], []
    # Gmail: ~15 years of mail, threads of 1-6, ~12% dropped with a reason.
    i = 0
    t = NOW
    while i < gmail:
        thread = f"g{i}"
        for _ in range(rng.choice((1, 1, 1, 2, 2, 3, 6))):
            t -= rng.randint(60_000, 7_200_000)
            if rng.random() < 0.12:
                dropped.append(("gmail", f"gmail-{i}", thread, t,
                                rng.choice(("category:promotions", "bulk:list_unsubscribe", "security:subject")),
                                SCRUBBER_VERSION))
            else:
                red = json.dumps({"EMAIL": 1}) if rng.random() < 0.2 else "{}"
                items.append(_item("gmail", i, "message", thread, t, f"Person {i % 900} <p{i % 900}@example.com>",
                                   _text(rng, 5), _text(rng, rng.randint(*body_words)), red=red))
            i += 1
    # iMessage: a few hundred chats, most of the traffic in a few of them.
    chats = [f"chat-{c}" for c in range(300)]
    weights = [1 / (c + 1) for c in range(300)]
    t = NOW
    for j in range(imessage):
        t -= rng.randint(10_000, 3_600_000)
        chat = rng.choices(chats, weights)[0]
        red = json.dumps({"PHONE": 1}) if rng.random() < 0.1 else "{}"
        items.append(_item("imessage", j, "chat", chat, t, f"+1555{j % 300:07d}", "", _text(rng, 12), red=red))
        if rng.random() < 0.03:
            dropped.append(("imessage", f"imessage-d{j}", chat, t - 1, "business_sender", SCRUBBER_VERSION))
    # Calendar: weekly series from 2018 into 2031, plus one-offs.
    start, end = NOW - 8 * 365 * DAY, NOW + 5 * 365 * DAY
    k = 0
    for series in range(gcal // 300 + 1):
        when = start + rng.randint(0, 365) * DAY
        while when < end and k < gcal * 0.8:
            items.append(_item("gcal", k, "event", f"primary/s{series}", when, "alex@example.org",
                               f"Weekly {rng.choice(WORDS)}", _text(rng, 8), end=when + 3_600_000))
            when += 7 * DAY * rng.choice((1, 1, 2))
            k += 1
    while k < gcal:
        when = rng.randint(start, end)
        items.append(_item("gcal", k, "event", f"primary/e{k}", when, "alex@example.org", _text(rng, 3),
                           _text(rng, 8), end=when + 3_600_000))
        k += 1
    with store._tx() as db:
        db.executemany("INSERT INTO items (source, id, account, kind, thread_id, date_ms, end_ms, sender, sender_email,"
                       " participants, title, body, attachments, attachment_names, link, extra, redactions,"
                       " scrubber_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", items)
        db.executemany("INSERT INTO dropped VALUES (?,?,?,?,?,?)", dropped)
    for src, extra in (("gmail", {"history_id": "1", "sync_mode": "full"}), ("imessage", {}), ("gcal", {})):
        store.set_state(src, "account", "alex@example.org" if src != "imessage" else "This Mac")
        store.set_state(src, "last_sync_ms", str(NOW))
        for k2, v in extra.items():
            store.set_state(src, k2, v)
    store.close()
    return data_dir / "mail.db"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--gmail", type=int, default=128_000)
    ap.add_argument("--imessage", type=int, default=40_000)
    ap.add_argument("--gcal", type=int, default=9_000)
    a = ap.parse_args()
    t0 = time.time()
    path = build(a.data_dir, a.gmail, a.imessage, a.gcal)
    print(f"built {path} in {time.time() - t0:.1f}s")
