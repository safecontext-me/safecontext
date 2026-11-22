import dataclasses

from safecontext.scrub import Attachment, Dropped, ScrubbedMessage, SCRUBBER_VERSION, scrub
from safecontext.scrub.models import RawMessage
from safecontext.demo.synthetic import mailbox, msg


def test_subject_redacted():
    r = scrub(msg(1, subject="Re: card 4111 1111 1111 1111", text="ok"))
    assert isinstance(r, ScrubbedMessage)
    assert r.subject == "Re: card [REDACTED:CREDIT_CARD]"


def test_attachment_metadata_only_and_filename_redacted():
    r = scrub(msg(1, text="see attached", attachments=[
        Attachment("passport_scan_SSN_536-22-1987.pdf", "application/pdf", 210334)]))
    (a,) = r.attachments
    assert "536" not in a.filename and a.filename.endswith(".pdf")
    assert a.mime_type == "application/pdf" and a.size == 210334
    assert {f.name for f in dataclasses.fields(Attachment)} == {"filename", "mime_type", "size"}


def test_raw_message_has_no_attachment_bytes_and_hides_repr():
    assert "data" not in {f.name for f in dataclasses.fields(RawMessage)}
    assert "SECRET" not in repr(msg(1, text="SECRET"))


def test_html_used_when_no_plain_text():
    r = scrub(msg(1, html="<p>Hello <b>Alex</b></p>"))
    assert r.body == "Hello Alex"


def test_plain_text_preferred():
    r = scrub(msg(1, text="plain version", html="<p>html version</p>"))
    assert r.body == "plain version"


def test_quote_stripped_before_store():
    r = scrub(msg(1, text="yes\n\nOn Mon, Nov 3, 2025 Alex <a@example.org> wrote:\n> SSN 536-22-1987"))
    assert r.body == "yes" and r.redactions == {}


def test_version_and_citation_fields():
    r = scrub(msg(7, text="hi"))
    assert r.scrubber_version == SCRUBBER_VERSION
    assert r.message_id_header == "<synthetic-7@example.com>"
    assert r.sender.startswith("Jamie") and r.date_ms > 0


class _FakeProse:
    def classify(self, paragraphs):
        return ["HEALTH" if "biopsy" in p else None for p in paragraphs]


def test_prose_classifier_hook():
    r = scrub(msg(1, text="Lunch Friday?\n\nThe biopsy came back and I'm worried.\n\nLet me know."),
              prose=_FakeProse())
    assert r.body == "Lunch Friday?\n\n[REDACTED:HEALTH]\n\nLet me know."
    assert r.redactions == {"HEALTH": 1}


def test_synthetic_mailbox_outcomes():
    results = {m.id: scrub(m) for m in mailbox()}
    kept = {k for k, v in results.items() if isinstance(v, ScrubbedMessage)}
    assert kept == {"m0001", "m0002", "m0003", "m0004", "m0005", "m0006"}
    reasons = {k: v.reason for k, v in results.items() if isinstance(v, Dropped)}
    assert reasons == {
        "m0007": "security:subject", "m0008": "security:subject", "m0009": "category:promotions",
        "m0010": "category:social", "m0011": "bulk:list_unsubscribe", "m0012": "bulk:no_reply_sender",
        "m0013": "sensitive:sender_domain", "m0014": "sensitive:subject", "m0015": "spam_trash",
        "m0016": "sensitive:sender_keyword",
    }
    assert results["m0006"].redactions == {}  # the clean control message is untouched
