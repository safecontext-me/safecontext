"""Normalization, HTML conversion, quote and signature stripping."""
from safecontext.scrub.normalize import html_to_text, normalize_text
from safecontext.scrub.quotes import strip_quoted, strip_signature


# --- normalization --------------------------------------------------------

def test_zero_width_removed():
    assert normalize_text("41​11 11‍11") == "4111 1111"


def test_fullwidth_digits_folded():
    assert normalize_text("４１１１") == "4111"


def test_nbsp_and_crlf():
    assert normalize_text("a b\r\nc") == "a b\nc"


def test_blank_lines_collapsed():
    assert normalize_text("a\n\n\n\n\nb") == "a\n\nb"


# --- HTML -----------------------------------------------------------------

def test_html_basic():
    out = normalize_text(html_to_text("<p>Hello&nbsp;<b>there</b></p><p>Line&#32;two<br>three</p>"))
    assert out == "Hello there\n\nLine two\nthree"


def test_html_drops_script_style_and_links():
    out = html_to_text('<style>.x{}</style><script>var k="secret"</script>'
                       '<a href="https://x.example/reset?token=abc">click</a>')
    assert "secret" not in out and "token" not in out and "click" in out


def test_html_gmail_signature_removed():
    out = html_to_text('<div>hi</div><div class="gmail_signature">Jamie<br>(415) 555-0132</div>')
    assert "555" not in out and "hi" in out


def test_html_blockquote_marked_as_quote():
    out = html_to_text("<div>new text</div><blockquote>old<br>older</blockquote>")
    assert "> old" in out and "> older" in out and "new text" in out


def test_html_malformed_does_not_crash():
    out = html_to_text("<div><p>unclosed <b>bold</div></i>tail")
    assert "unclosed" in out and "tail" in out


# --- quoted replies -------------------------------------------------------

def test_gmail_reply_header_cut():
    text = "Sounds good\n\nOn Mon, Nov 3, 2025 at 9:00 AM Alex Kim <alex@example.org> wrote:\n> old"
    assert strip_quoted(text) == "Sounds good"


def test_wrapped_reply_header_cut():
    text = "Yes!\n\nOn Mon, Nov 3, 2025 at 9:00 AM Alex Kim <\nalex@example.org> wrote:\n> old"
    assert strip_quoted(text) == "Yes!"


def test_outlook_reply_cut():
    text = "Agreed.\n\nFrom: Alex Kim <alex@example.org>\nSent: Monday, November 3, 2025 9:00 AM\nTo: me\nold body"
    assert strip_quoted(text) == "Agreed."


def test_original_message_marker_cut():
    assert strip_quoted("ok\n-----Original Message-----\nold") == "ok"


def test_inline_quote_lines_removed():
    assert strip_quoted("> earlier point\nmy answer\n> another\nmore") == "my answer\nmore"


def test_forwarded_message_kept():
    text = ("FYI see below\n\n---------- Forwarded message ---------\n"
            "From: Sam <sam@example.dev>\nDate: Mon, Nov 3, 2025\nSubject: plan\n\nThe plan is X.")
    assert "The plan is X." in strip_quoted(text)


# --- signatures -----------------------------------------------------------

def test_sig_delimiter():
    assert strip_signature("body\n-- \nSam | (415) 555-0199") == "body"


def test_mobile_footer():
    assert strip_signature("ok see you\n\nSent from my iPhone") == "ok see you"


def test_valediction_block_trimmed_to_name():
    text = "Let's meet Tuesday.\n\nBest,\nJamie\nProduct Lead\nExample Co\n742 Evergreen Terrace"
    assert strip_signature(text) == "Let's meet Tuesday.\n\nBest,\nJamie"


def test_long_trailing_content_not_treated_as_signature():
    body = "Thanks!\n" + "\n".join(f"point {i}" for i in range(12))
    assert strip_signature(body) == body
