"""Synthetic mailbox. Every value here is fake: test card numbers, documented
example keys, 555-01xx phone numbers, fictional addresses, example domains."""
from __future__ import annotations

from typing import List, Tuple

from safecontext.scrub import Attachment, RawMessage


def _pad(prefix: str, n: int, fill: str = "Canary0123456789") -> str:
    body = (fill * 10)[:n]
    return prefix + body


# (phrase template, secret). The secret is what must never reach disk.
CANARIES: List[Tuple[str, str]] = [
    ("my card is {}", "4111 1111 1111 1111"),
    ("amex {}", "378282246310005"),
    ("mastercard {}", "5555-5555-5555-4444"),
    ("SSN {}", "536-22-1987"),
    ("social security number: {}", "536221987"),
    ("key {}", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_secret_access_key = {}", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("OPENAI_API_KEY={}", "sk-proj-" + _pad("", 40)),
    ("XAI_API_KEY={}", _pad("xai-", 40)),
    ("token {}", _pad("ghp_", 36)),
    ("pat {}", _pad("github_pat_", 40, "Canary_0123456789")),
    ("slack {}", "xoxb-1234567890-" + _pad("", 20)),
    ("maps key {}", _pad("AIza", 35)),
    ("stripe {}", _pad("sk_live_", 24)),
    ("jwt {}", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"),
    ("password: {}", "Hunter2!Canary"),
    ("Your code is {}", "482913"),
    ("routing number {}", "021000021"),
    ("account number {}", "000123456789"),
    ("IBAN {}", "GB82 WEST 1234 5698 7654 32"),
    ("IBAN: {}", "DE89370400440532013000"),
    ("DOB: {}", "03/14/1985"),
    ("call me at {}", "(415) 555-0132"),
    ("cell {}", "+1 212-555-0187"),
    ("I live at {}", "742 Evergreen Terrace, Springfield, IL 62704"),
    ("CVV {}", "737"),
    ("ship it to {}", "Portland, OR 97205"),
    ("mail to {}", "PO Box 4417"),
    # 2FA (top priority)
    ("{} is your Instagram code", "593017"),
    ("login code:\n\n{}", "771204"),
    ("one-time passcode {}", "K7X-9QP"),
    ("setup key: {}", "JBSW Y3DP EHPK 3PXP"),
    ("otpauth://totp/Ex:alex?secret={}&issuer=Ex", "KRSXG5CTMVRXEZLU"),
    ("backup codes:\n{}", "8412 5590"),
]
TWO_FA_SECRETS = ["593017", "771204", "K7X-9QP", "JBSW Y3DP EHPK 3PXP", "JBSWY3DPEHPK3PXP",
                  "KRSXG5CTMVRXEZLU", "8412 5590", "84125590"]

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCanaryPrivateKeyBody0123456789\n"
    "q2Vz9CanaryKeyLineTwoAbCdEfGhIjKlMnOpQrStUv\n"
    "-----END RSA PRIVATE KEY-----"
)
PRIVATE_KEY_SECRETS = ["CanaryPrivateKeyBody0123456789", "CanaryKeyLineTwo"]

BENIGN_MARKER = "BENIGN-MARKER-kept-content"


def needles(secret: str) -> List[str]:
    """Strings whose presence on disk would mean a leak."""
    out = [secret]
    digits = "".join(c for c in secret if c.isdigit())
    if len(digits) >= 6 and digits != secret:
        out.append(digits)
    return out


def all_needles() -> List[str]:
    out: List[str] = []
    for _, s in CANARIES:
        # 3-digit CVV and 6-digit OTP are too short to search raw bytes for
        # without false hits; they are covered by contextual needles below.
        if len("".join(c for c in s if c.isalnum())) >= 7:
            out += needles(s)
    out += ["CVV 737", "code is 482913"]
    # Multi-part values: each part must be gone, not just the whole string.
    out += ["742 Evergreen", "Evergreen Terrace", "IL 62704", "OR 97205", "Box 4417"]
    out += PRIVATE_KEY_SECRETS
    out += TWO_FA_SECRETS
    return out


def msg(i, *, frm="Jamie Rivera <jamie@example.com>", subject="Catching up", text="", html="",
        labels=("INBOX",), extra_headers=None, attachments=()) -> RawMessage:
    headers = {
        "from": frm,
        "to": "Alex Kim <alex@example.org>",
        "subject": subject,
        "date": "Mon, 3 Nov 2025 10:00:00 -0800",
        "message-id": f"<synthetic-{i}@example.com>",
    }
    headers.update({k.lower(): v for k, v in (extra_headers or {}).items()})
    return RawMessage(
        id=f"m{i:04d}", thread_id=f"t{i:04d}", label_ids=list(labels), headers=headers,
        text_body=text, html_body=html, attachments=list(attachments),
        internal_date_ms=1762192800000 + i * 60000,
    )


def _zw(s: str) -> str:
    return "​".join(s[i:i + 3] for i in range(0, len(s), 3))


def _fullwidth_digits(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if c.isdigit() else c for c in s)


def leak_matrix() -> List[RawMessage]:
    """Every canary in every context a secret can hide in."""
    out: List[RawMessage] = []
    i = 1000
    for tmpl, secret in CANARIES:
        phrase = tmpl.format(secret)
        contexts = {
            "plain": dict(text=f"Hi Alex,\n\n{phrase}\n\n{BENIGN_MARKER}\n\nJamie"),
            "html": dict(html=f"<html><body><p>Hi Alex,</p><p>{phrase}</p><p>{BENIGN_MARKER}</p></body></html>"),
            "zero_width": dict(text=f"Note: {_zw(phrase)}\n\n{BENIGN_MARKER}"),
            "fullwidth": dict(text=f"Note: {_fullwidth_digits(phrase)}\n\n{BENIGN_MARKER}"),
            "quoted": dict(text=f"Sounds good!\n\nOn Mon, Nov 3, 2025 at 9:00 AM Alex Kim <alex@example.org> wrote:\n> {phrase}\n> {BENIGN_MARKER}"),
            "signature": dict(text=f"See you then.\n\nBest,\nJamie\nProduct Lead\n{phrase}"),
            "subject": dict(subject=f"Re: plans {phrase}", text=f"{BENIGN_MARKER}"),
        }
        for _, kw in contexts.items():
            i += 1
            out.append(msg(i, **kw))
        if sum(c.isdigit() for c in secret) >= 9:
            i += 1
            out.append(msg(i, text=f"{BENIGN_MARKER}", attachments=[
                Attachment(f"scan_{tmpl.format(secret).replace(' ', '_')}.pdf", "application/pdf", 48213)]))
    i += 1
    out.append(msg(i, text=f"Here's the deploy key:\n\n{PRIVATE_KEY}\n\n{BENIGN_MARKER}"))
    i += 1
    out.append(msg(i, html="<p>key below</p><pre>" + PRIVATE_KEY + "</pre>"))
    return out


def mailbox() -> List[RawMessage]:
    """A small, realistic mailbox for the before/after report."""
    return [
        msg(1, subject="Dinner Saturday?", text=(
            "Hey Alex,\n\nAre we still on for Saturday? I can pick you up around 7.\n\n"
            "Also, can you pay me back for the tickets? My card is 4111 1111 1111 1111 if you want "
            "to just put it on there, ha. Call me at (415) 555-0132 if plans change.\n\n"
            "Thanks,\nJamie\nProduct Lead, Example Co\n742 Evergreen Terrace, Springfield, IL 62704\n"
            "+1 212-555-0187\n\n"
            "On Fri, Oct 31, 2025 at 5:12 PM Alex Kim <alex@example.org> wrote:\n"
            "> wifi password: Hunter2!Canary\n> see you soon\n")),
        msg(2, frm="Sam Patel <sam@example.dev>", subject="staging creds", text=(
            "Here's what you need for staging. Rotate these after!\n\n"
            "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
            "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
            "OPENAI_API_KEY=sk-proj-" + _pad("", 40) + "\n"
            "XAI_API_KEY=" + _pad("xai-", 40) + "\n"
            "GITHUB_TOKEN=" + _pad("ghp_", 36) + "\n"
            "STRIPE_KEY=" + _pad("sk_live_", 24) + "\n"
            "session: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ\n\n"
            + PRIVATE_KEY + "\n\n-- \nSam Patel | Staff Engineer | (415) 555-0199\n")),
        msg(3, frm="Morgan Lee <morgan@example.net>", subject="Rent split",
            html=("<div>Hi! For the rent transfer use:</div><div>IBAN: DE89​3704​0044​0532​0130​00</div>"
                  "<div>or US: routing number 021000021, account number 000123456789</div>"
                  "<div class=\"gmail_signature\">Morgan Lee<br>Mobile: (415) 555-0144</div>"
                  "<div class=\"gmail_quote\">On Thu, Morgan wrote:<blockquote>old stuff 536-22-1987</blockquote></div>")),
        msg(4, frm="Pat Doe <pat@example.com>", subject="Form for the trip", text=(
            "Filled in the form for you: name Alex Kim, DOB: 03/14/1985, SSN 536-22-1987.\n"
            "Let me know if anything's wrong.")),
        msg(5, frm="Jamie Rivera <jamie@example.com>", subject="netflix",
            text="It's asking for a code, can you read it to me? Your code is 482913 apparently.",
            attachments=[Attachment("passport_scan_SSN_536-22-1987.pdf", "application/pdf", 210334),
                         Attachment("boarding-pass.png", "image/png", 88120)]),
        msg(6, frm="Taylor Wu <taylor@example.org>", subject="Notes from Tuesday", text=(
            "Notes from the planning meeting:\n- Ship v2.4.1 by Nov 14\n- Budget is $1,299.00 per seat\n"
            "- Order #112-4456789-1234567 arrived\n- Standup moves to 10:30\n\nSent from my iPhone")),
        msg(7, frm="Accounts <security@example-app.com>", subject="Your verification code",
            text="Your verification code is 771204. It expires in 10 minutes."),
        msg(8, frm="Example App <support@example-app.com>", subject="Reset your password",
            text="Click the link to reset your password."),
        msg(9, frm="Deals <deals@shop.example>", subject="48-hour sale!",
            text="Everything 40% off.", labels=("INBOX", "CATEGORY_PROMOTIONS")),
        msg(10, frm="Friendbook <notify@friendbook.example>", subject="You have 3 new friend requests",
            text="See who wants to connect.", labels=("INBOX", "CATEGORY_SOCIAL")),
        msg(11, frm="The Weekly <editor@newsletter.example>", subject="This week in tech",
            text="Top stories...", extra_headers={"List-Unsubscribe": "<mailto:unsub@newsletter.example>"}),
        msg(12, frm="Shop <no-reply@shop.example>", subject="Your order has shipped",
            text="Tracking: 1Z999AA10123456784"),
        msg(13, frm="Chase <alerts@chase.com>", subject="Your statement is ready",
            text="Your statement for account ending 4444 is available."),
        msg(14, frm="HR <hr@acme-corp.example>", subject="Your 2025 W-2 is available",
            text="Log in to download your W-2."),
        msg(15, frm="Prize Dept <winner@scam.example>", subject="You won!!!",
            text="Send your bank details.", labels=("SPAM",)),
        msg(16, frm="Lab Portal <results@citylabhealth.example>", subject="New message",
            text="A new message is waiting in your portal."),
    ]
