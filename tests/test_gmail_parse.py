import base64

from safecontext.gmail_parse import MAX_TEXT_CHARS, history_message_ids, parse_gmail_message


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def part(mime, data=b"", filename="", headers=None, **body):
    b = {"size": len(data), **body}
    if data:
        b["data"] = b64(data)
    return {"mimeType": mime, "filename": filename, "headers": headers or [], "body": b}


def resource(payload, **kw):
    return {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"], "internalDate": "1762192800000",
            "snippet": "RAW SNIPPET 4111 1111 1111 1111", "payload": payload, **kw}


def test_multipart_alternative():
    p = {"mimeType": "multipart/alternative", "headers": [{"name": "Subject", "value": "Hi"},
                                                         {"name": "From", "value": "a@example.com"}],
         "parts": [part("text/plain", b"plain body"), part("text/html", b"<p>html body</p>")]}
    raw = parse_gmail_message(resource(p))
    assert raw.text_body == "plain body" and raw.html_body == "<p>html body</p>"
    assert raw.header("subject") == "Hi" and raw.internal_date_ms == 1762192800000


def test_snippet_is_ignored():
    raw = parse_gmail_message(resource({"mimeType": "text/plain", "headers": [], "body": {"size": 0}}))
    assert "4111" not in raw.text_body + raw.html_body


def test_attachments_are_metadata_only_even_with_inline_data():
    secret = b"INLINE-ATTACHMENT-SECRET 536-22-1987"
    p = {"mimeType": "multipart/mixed", "headers": [], "parts": [
        part("text/plain", b"see attached"),
        part("application/pdf", filename="scan.pdf", attachmentId="ANGjdJ8", size=48213),
        part("text/plain", secret, filename="notes.txt"),      # small attachment delivered inline
        part("image/png", b"\x89PNG", filename="logo.png", headers=[
            {"name": "Content-Disposition", "value": "inline"}]),
        part("text/calendar", b"BEGIN:VCALENDAR\nSUMMARY:Oncology appt"),  # invite, no filename
    ]}
    raw = parse_gmail_message(resource(p))
    assert raw.text_body == "see attached"
    names = [(a.filename, a.mime_type) for a in raw.attachments]
    assert names == [("scan.pdf", "application/pdf"), ("notes.txt", "text/plain"),
                     ("logo.png", "image/png"), ("(unnamed)", "text/calendar")]
    assert raw.attachments[0].size == 48213
    everything = raw.text_body + raw.html_body + repr(raw.attachments)
    assert "SECRET" not in everything and "Oncology" not in everything


def test_forwarded_eml_not_descended_into():
    inner = {"mimeType": "message/rfc822", "filename": "fwd.eml", "headers": [], "body": {"size": 900},
             "parts": [part("text/plain", b"forwarded secret body")]}
    raw = parse_gmail_message(resource({"mimeType": "multipart/mixed", "headers": [],
                                        "parts": [part("text/plain", b"fyi"), inner]}))
    assert raw.text_body == "fyi" and raw.attachments[0].filename == "fwd.eml"


def test_nested_multipart():
    p = {"mimeType": "multipart/mixed", "headers": [], "parts": [
        {"mimeType": "multipart/related", "headers": [], "parts": [
            {"mimeType": "multipart/alternative", "headers": [], "parts": [
                part("text/plain", b"deep text"), part("text/html", b"<b>deep</b>")]}]}]}
    raw = parse_gmail_message(resource(p))
    assert raw.text_body == "deep text" and raw.html_body == "<b>deep</b>"


def test_charset_latin1():
    p = part("text/plain", "café crème".encode("latin-1"),
             headers=[{"name": "Content-Type", "value": 'text/plain; charset="ISO-8859-1"'}])
    assert parse_gmail_message(resource(p)).text_body == "café crème"


def test_unknown_charset_falls_back():
    p = part("text/plain", b"hello", headers=[{"name": "Content-Type", "value": "text/plain; charset=x-bogus"}])
    assert parse_gmail_message(resource(p)).text_body == "hello"


def test_leftover_quoted_printable_decoded():
    p = part("text/plain", b"card 4111 1111 =\r\n1111 1111 and a=3Db", headers=[
        {"name": "Content-Transfer-Encoding", "value": "quoted-printable"}])
    assert parse_gmail_message(resource(p)).text_body == "card 4111 1111 1111 1111 and a=b"


def test_corrupt_base64_is_empty_not_crash():
    p = {"mimeType": "text/plain", "headers": [], "body": {"size": 3, "data": "!!!"}}
    assert parse_gmail_message(resource(p)).text_body == ""


def test_huge_body_capped():
    p = part("text/plain", b"a" * (MAX_TEXT_CHARS + 10))
    assert len(parse_gmail_message(resource(p)).text_body) == MAX_TEXT_CHARS


def test_metadata_only_has_no_body():
    p = {"mimeType": "multipart/mixed", "headers": [{"name": "Subject", "value": "S"}],
         "parts": [part("text/plain", b"body")]}
    raw = parse_gmail_message(resource(p), metadata_only=True)
    assert raw.text_body == "" and raw.attachments == [] and raw.header("subject") == "S"


def test_duplicate_headers_first_wins():
    p = {"mimeType": "text/plain", "body": {"size": 0}, "headers": [
        {"name": "Subject", "value": "first"}, {"name": "subject", "value": "second"}]}
    assert parse_gmail_message(resource(p)).header("subject") == "first"


def test_history_ids():
    page = {"history": [
        {"id": "1", "messagesAdded": [{"message": {"id": "a"}}]},
        {"id": "2", "labelsAdded": [{"message": {"id": "b"}, "labelIds": ["TRASH"]}]},
        {"id": "3", "labelsRemoved": [{"message": {"id": "c"}, "labelIds": ["INBOX"]}]},
        {"id": "4", "messagesDeleted": [{"message": {"id": "d"}}]},
    ]}
    assert history_message_ids(page) == ({"a", "b", "c"}, {"d"})
