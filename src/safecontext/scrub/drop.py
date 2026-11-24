"""Layer 1: decide whether a message is excluded entirely.

Each check returns a reason string or None. Lists are deliberately
conservative toward dropping: a wrongly dropped newsletter costs little,
a wrongly kept bank statement is the failure this product exists to prevent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Callable, Dict, FrozenSet, Optional, Tuple, Union

from .models import KIND_CHAT, KIND_MESSAGE, RawItem, RawMessage
from .otp import looks_like_security_mail

SPAM_TRASH_LABELS = frozenset({"SPAM", "TRASH"})
CATEGORY_LABELS = {
    "CATEGORY_PROMOTIONS": "category:promotions",
    "CATEGORY_SOCIAL": "category:social",
    "CATEGORY_FORUMS": "category:forums",
}

# Delivery-failure notices. No-reply senders are deliberately not dropped:
# they send receipts, bookings and shipping notices people want their AI to
# see, and the risky ones (sign-in codes, statements, health notices) are
# caught by the security and sensitive-sender rules whatever the address.
_BOUNCE_LOCAL = re.compile(r"^(?:mailer-daemon|postmaster|bounces?)(?:[-+_.].*)?$", re.I)

_SECURITY_SUBJECT = re.compile(
    r"\b(?:verification code|verify your (?:email|account|identity)|confirm your (?:email|account)|"
    r"one[- ]time (?:pass(?:word|code)|code)|\bOTP\b|security code|sign[- ]?in code|login code|"
    r"2-step|two[- ]factor|2FA|MFA|password reset|reset your password|"
    r"new sign[- ]?in|security alert|suspicious (?:sign[- ]?in|activity)|magic link|"
    r"your (?:login|sign[- ]?in) link|recovery (?:code|email)|backup codes?|access code|"
    r"authentication (?:code|request)|login attempt|confirm it'?s you|(?:sign|log) ?in to|"
    r"G-\d{5,8}|\d{4,8} is your|your (?:\w+ )?code(?: is\b|:|$))",
    re.I,
)
# Account-security notices: something gained access to, or changed how you
# sign in to, an account. Each phrase pairs the thing (an app, device, key,
# recovery address, 2FA) with what happened to it, and most need "your", so a
# newsletter about API keys or a new phone launch doesn't match. Used on the
# subject and the body.
_ACCOUNT_SECURITY = re.compile(
    r"\bthird[- ]party (?:OAuth )?(?:application|app)s?\b[^.\n]{0,80}?\b(?:added|authorized|granted)\b|"
    r"\b(?:has been|was|were) (?:authorized|granted permission) to access your\b|"
    r"\b(?:OAuth )?(?:app|application|integration)s? (?:[\w'-]+ ){0,3}?(?:has been |was |were |is )?"
    r"(?:authorized|connected|added|granted access) (?:to|for|on) your\b|"
    r"\bgranted access to your (?:\w+ )?account|"
    r"\b(?:sign(?:ed)?|log(?:ged)?) in (?:on|to|from|with) a new (?:device|browser|computer|phone|location)|"
    r"\bnew (?:device|browser|computer|phone)s? (?:[\w'-]+ ){0,3}?(?:added|signed in|logged in|registered|"
    r"connected)(?: to| on| for)? (?:to |on |for )?your\b|"
    r"\bnew (?:device|browser|login|log[- ]in) (?:sign[- ]?in|login|alert|detected)|"
    r"\b(?:SSH|GPG|PGP|deploy|API|access|public|security|signing)(?: [\w-]+){0,3}? (?:key|token|passkey)s? "
    r"(?:has been |was |were )?(?:added|created|generated|registered) (?:to|for|on) your\b|"
    r"\b(?:a )?(?:new )?passkey (?:has been |was )?(?:added|created|removed)|"
    r"\byour (?:recovery|backup|secondary|alternate) (?:email|phone)(?: address| number)? "
    r"(?:has been |was )?(?:changed|updated|added|removed)|"
    r"\byour (?:email address|phone number|password|username|sign[- ]?in email) (?:has been |was )(?:changed|updated)|"
    r"\b(?:two[- ]factor|2-step|two[- ]step|2FA|multi[- ]factor|MFA)(?: authentication| verification)? "
    r"(?:has been |was |is (?:now )?)(?:turned on|turned off|enabled|disabled|removed)",
    re.I,
)
_PASSWORD_RESET_BODY = re.compile(r"\breset your password\b|\bpassword reset (?:link|request)\b", re.I)

DEFAULT_SENSITIVE_DOMAINS: FrozenSet[str] = frozenset({
    # banking / payments / brokerage
    "chase.com", "bankofamerica.com", "bofa.com", "wellsfargo.com", "citi.com", "citibank.com",
    "capitalone.com", "americanexpress.com", "aexp.com", "discover.com", "usbank.com", "pnc.com",
    "truist.com", "tdbank.com", "ally.com", "sofi.com", "chime.com", "schwab.com", "fidelity.com",
    "vanguard.com", "etrade.com", "robinhood.com", "paypal.com", "venmo.com", "zellepay.com",
    "wise.com", "coinbase.com", "navyfederal.org", "usaa.com", "marcus.com", "synchronybank.com",
    # tax
    "irs.gov", "ssa.gov", "intuit.com", "turbotax.com", "hrblock.com", "taxact.com", "freetaxusa.com",
    # health / insurance / pharmacy
    "mychart.com", "mychart.org", "kp.org", "kaiserpermanente.org", "cvs.com", "cvshealth.com",
    "walgreens.com", "labcorp.com", "questdiagnostics.com", "zocdoc.com", "onemedical.com",
    "anthem.com", "aetna.com", "cigna.com", "uhc.com", "bcbs.com", "humana.com", "goodrx.com",
    "teladoc.com", "betterhelp.com", "talkspace.com", "23andme.com", "ancestry.com",
})

# Dating apps: who someone is matching with and messaging is as private as
# health. Subdomains count too (mail.hinge.co).
DEFAULT_DATING_DOMAINS: FrozenSet[str] = frozenset({
    "hinge.co", "tinder.com", "gotinder.com", "bumble.com", "match.com", "okcupid.com", "grindr.com",
    "her.app", "weareher.com", "feeld.co", "coffeemeetsbagel.com", "raya.app", "rayatheapp.com",
    "eharmony.com", "zoosk.com", "pof.com", "plentyoffish.com", "hily.com", "theleague.com", "jdate.com",
    "christianmingle.com", "elitesingles.com", "scruff.com", "taimi.com", "lex.app", "archer.app",
    "badoo.com", "happn.com", "tantanapp.com", "boo.world", "thursday.social",
})
_DATING_DOMAIN_TOKENS = re.compile(r"dating|singles\b", re.I)

# Tokens inside a sender domain's labels that signal a sensitive institution.
_SENSITIVE_DOMAIN_TOKENS = re.compile(
    r"bank|creditunion|fcu\b|federalcu|clinic|hospital|pharmacy|medical|health|dental|"
    r"insurance|mortgage|lending|\btax",
    re.I,
)

_SENSITIVE_SUBJECT = re.compile(
    r"\b(?:(?:account|bank|card|monthly|e-?|billing|brokerage|credit card) ?statements?|"
    r"statement is (?:ready|available)|tax (?:return|document|form|refund)s?|"
    r"1099(?:-[A-Z]+)?|W-?2|1040|1098|K-1|IRS\b|"
    r"direct deposit|wire transfer|routing number|pay ?stub|payslip|paycheck|"
    r"lab (?:results?|report)|test results?|diagnosis|prescription|"
    r"explanation of benefits|\bEOB\b|medical records?|insurance claim|claim status|"
    r"appointment (?:reminder|confirmation)|patient portal|health (?:record|summary))\b",
    re.I,
)


@dataclass(frozen=True)
class DropConfig:
    sensitive_domains: FrozenSet[str] = field(default_factory=lambda: DEFAULT_SENSITIVE_DOMAINS)
    dating_domains: FrozenSet[str] = field(default_factory=lambda: DEFAULT_DATING_DOMAINS)
    allow_domains: FrozenSet[str] = frozenset()  # user overrides: never drop for sender reasons
    drop_categories: bool = True
    drop_bulk: bool = True


DEFAULT_CONFIG = DropConfig()


def _sender_domain(from_header: str) -> str:
    _, addr = parseaddr(from_header)
    return addr.rpartition("@")[2].lower().strip(">. ")


def _sender_local(from_header: str) -> str:
    _, addr = parseaddr(from_header)
    return addr.rpartition("@")[0].lower()


def _domain_matches(domain: str, domains: FrozenSet[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def check_labels(msg: RawMessage, config: DropConfig = DEFAULT_CONFIG) -> Optional[str]:
    labels = set(msg.label_ids)
    if labels & SPAM_TRASH_LABELS:
        return "spam_trash"
    if "DRAFT" in labels:
        return "draft"
    if "CHAT" in labels:
        return "chat"
    if config.drop_categories:
        for label, reason in CATEGORY_LABELS.items():
            if label in labels:
                return reason
    return None


def check_bulk(msg: RawMessage, config: DropConfig = DEFAULT_CONFIG) -> Optional[str]:
    if not config.drop_bulk:
        return None
    if msg.header("list-unsubscribe"):
        return "bulk:list_unsubscribe"
    if msg.header("precedence").strip().lower() in {"bulk", "junk"}:
        return "bulk:precedence"
    if _BOUNCE_LOCAL.match(_sender_local(msg.header("from"))):
        return "bulk:bounce"
    return None


def check_sensitive_sender(msg: RawMessage, config: DropConfig = DEFAULT_CONFIG) -> Optional[str]:
    domain = _sender_domain(msg.header("from"))
    if not domain or _domain_matches(domain, config.allow_domains):
        return None
    if _domain_matches(domain, config.sensitive_domains):
        return "sensitive:sender_domain"
    # Only look at the registrable part, not the TLD, for keyword tokens.
    labels = domain.split(".")[:-1]
    if _domain_matches(domain, config.dating_domains) or any(_DATING_DOMAIN_TOKENS.search(l) for l in labels):
        return "sensitive:dating"
    if any(_SENSITIVE_DOMAIN_TOKENS.search(label) for label in labels):
        return "sensitive:sender_keyword"
    return None


def check_subject(subject: str) -> Optional[str]:
    if _SECURITY_SUBJECT.search(subject) or _ACCOUNT_SECURITY.search(subject):
        return "security:subject"
    if _SENSITIVE_SUBJECT.search(subject):
        return "sensitive:subject"
    return None


def check_security_body(body: str) -> Optional[str]:
    if looks_like_security_mail(body) or _PASSWORD_RESET_BODY.search(body) or _ACCOUNT_SECURITY.search(body):
        return "security:body"
    return None


# --- chats (iMessage, SMS, WhatsApp, ...) -------------------------------------
#
# Text messages have no sender domain, so check_sensitive_sender can't see that a
# text is from a bank or a pharmacy. Two chat-only rules close that gap:
#
#  * business_sender: messages from shortcodes, alphanumeric sender IDs and
#    US/CA toll-free numbers are automated (banks, pharmacies, delivery, 2FA...).
#    All of them are dropped, even when the number is saved in Contacts, since
#    people save their bank. Phone numbers and email/iMessage addresses are people.
#  * sensitive_text: from anyone, text that clearly is a bank, tax or health
#    notice (balance or transaction alerts, "card ending in", prescription ready,
#    results ready, appointment reminders, EOBs, "Chase: ..." style prefixes).
#    The bar is a phrase such a notice uses, not a single word, so a friend's
#    "meet you at the bank" stays.

_TOLL_FREE_US = frozenset({"800", "833", "844", "855", "866", "877", "888"})
_PHONE_CHARS = re.compile(r"[\s().\-]")


def is_business_handle(handle: str) -> bool:
    """Shortcode, alphanumeric sender ID or US/CA toll-free number."""
    h = (handle or "").strip()
    if not h or "@" in h:  # nothing to go on, or an email / iMessage address: a person
        return False
    compact = _PHONE_CHARS.sub("", h)
    if re.fullmatch(r"\d{3,8}", compact):  # shortcode
        return True
    if re.fullmatch(r"\+?\d{9,15}", compact):  # a phone number: business only if toll-free
        digits = compact.lstrip("+")
        national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
        return len(national) == 10 and national[:3] in _TOLL_FREE_US
    return bool(re.search(r"[A-Za-z]", h))  # alphanumeric sender ID ("CVS", "AMZNSMS", urn:biz:...)


def check_business_sender(item: Item, config: DropConfig = DEFAULT_CONFIG) -> Optional[str]:
    handle = getattr(item, "sender_handle", None)
    if handle is None:
        handle = item.header("from")
    return "business_sender" if is_business_handle(handle) else None


# A Contacts name that says what the sender is ("Bright Smile Dental", "Chase Bank").
_SENSITIVE_NAME = re.compile(
    r"\b(?:bank|banking|credit union|clinic|hospital|pharmacy|medical|dental|dentist|orthodontics?|"
    r"insurance|mortgage|urgent care|health ?care|tax (?:service|prep|preparer)s?)\b", re.I)


def check_chat_sender_name(item: Item, config: DropConfig = DEFAULT_CONFIG) -> Optional[str]:
    handle = getattr(item, "sender_handle", None)
    name = item.header("from") if handle is not None else ""  # only a Contacts name, never a handle
    return "sensitive:sender_keyword" if name and _SENSITIVE_NAME.search(name) else None


def _brand_keys(domains: FrozenSet[str]) -> FrozenSet[str]:
    """"chase.com" -> "chase": the sensitive sender list, as names a text may start with."""
    return frozenset(d.split(".")[0] for d in domains) | {
        "amex", "americanexpress", "bofa", "bankofamerica", "wellsfargo", "capitalone", "kaiser",
        "cvspharmacy", "unitedhealthcare", "questdiagnostics", "hrblock", "irs", "etrade"}


_BRAND_KEYS = _brand_keys(DEFAULT_SENSITIVE_DOMAINS)
# "Chase: ..." / "CVS Pharmacy alert ..." and the text is about money, an account or health.
# (A brand name alone isn't enough: a friend may write "Chase: you coming?" in a group.)
_TEXT_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 &.'*-]{1,30}?)\s*(?::|\balert\b)", re.I)
_NOTICE_WORDS = re.compile(r"\$\s?\d|\b(?:account|card|balance|payment|deposit|withdrawal|transfer|statement|"
                           r"alert|fraud|prescription|rx|refill|appointment|claim|results?)\b", re.I)

_AMOUNT = r"\$\s?\d[\d,]*(?:\.\d\d)?"
_SENSITIVE_TEXT = re.compile("|".join([
    # card / account alerts
    r"\b(?:card|account|acct)\b[^.\n]{0,20}\bend(?:ing|s)(?: in| with)?\s*[x*#.]*\d{2,4}\b",
    r"\bbalance\b[^\n]{0,30}?\b(?:is|of|was|:)\s*" + _AMOUNT,
    r"\bbalance\b[^\n]{0,40}\bas of\b",
    r"\b(?:transaction|purchase|charge|withdrawal|debit|deposit)\b[^\n]{0,60}\b(?:on|to|from|in) your\b"
    r"(?: \w+){0,2} (?:card|account)\b",
    r"\byour\b(?: \w+){0,2} (?:card|account)\b[^\n]{0,30}\b(?:was|has been) (?:charged|debited|credited)\b",
    r"\b(?:statement|tax (?:form|document|return|refund)s?)\b[^\n]{0,20}\b(?:is|are) (?:ready|available)\b",
    # health
    r"\b(?:prescriptions?|rx|refills?)\b[^\n]{0,60}\b(?:is|are) (?:now )?(?:ready|being filled|filled|due|available)\b",
    r"\bready for pick ?up\b[^\n]{0,40}\b(?:prescription|rx|pharmacy)\b",
    r"\brefill (?:request|reminder|is due)\b",
    r"\b(?:lab|test) results?\b[^\n]{0,40}\b(?:is|are) (?:now )?(?:ready|available|in|posted)\b",
    r"\bnew (?:lab |test )?results?\b[^\n]{0,40}\b(?:portal|mychart)\b",
    r"\breminder\b[^\n]{0,40}\bappointment\b",
    r"\byour(?: \w+){0,2} (?:appointment|appt)\b[^\n]{0,10}\b(?:with|at|on|is|for)\b",
    r"\b(?:appointment|appt)\b[^\n]{0,80}\breply\s+(?:C|Y|YES|1|CONFIRM)\b",
    r"\bexplanation of benefits\b|\bEOB\b",
]), re.I)


def check_sensitive_text(text: str, config: DropConfig = DEFAULT_CONFIG) -> Optional[str]:
    """A chat whose text is a bank, tax or health notice. See the note above."""
    if not text:
        return None
    m = _TEXT_PREFIX.match(text)
    if m and re.sub(r"[^a-z]", "", m.group(1).lower()) in _BRAND_KEYS and _NOTICE_WORDS.search(text):
        return "sensitive:text"
    return "sensitive:text" if _SENSITIVE_TEXT.search(text) else None


# --- which rules apply to which kind of item --------------------------------
#
# Email-only rules read things only email has: Gmail labels (spam, drafts,
# Promotions…), bulk-mail headers (List-Unsubscribe, Precedence) and bounce
# senders (mailer-daemon). A chat or a calendar invite never carries them.
#
# Universal rules protect what the product exists to protect, so they apply to
# every source: security / one-time-code content, and banking, tax and health
# senders and subjects (titles). Inline redaction (codes, cards, accounts,
# addresses, …) also runs on every item; see pipeline.scrub_item.

EMAIL_ONLY_RULES: Tuple[str, ...] = ("labels", "bulk")
UNIVERSAL_RULES: Tuple[str, ...] = ("sensitive_sender", "subject", "security_body")
# Chat-only. "sensitive_text" needs the normalized body, so pipeline.scrub_item runs it.
CHAT_ONLY_RULES: Tuple[str, ...] = ("business_sender", "sensitive_sender_name", "sensitive_text")
EMAIL_KINDS = frozenset({KIND_MESSAGE})
CHAT_KINDS = frozenset({KIND_CHAT})

Item = Union[RawMessage, RawItem]
_HEADER_RULES: Dict[str, Callable[[Item, str, DropConfig], Optional[str]]] = {
    "labels": lambda m, subject, cfg: check_labels(m, cfg),
    "bulk": lambda m, subject, cfg: check_bulk(m, cfg),
    "sensitive_sender": lambda m, subject, cfg: check_sensitive_sender(m, cfg),
    "subject": lambda m, subject, cfg: check_subject(subject),
    "business_sender": lambda m, subject, cfg: check_business_sender(m, cfg),
    "sensitive_sender_name": lambda m, subject, cfg: check_chat_sender_name(m, cfg),
}


def rules_for(kind: str) -> Tuple[str, ...]:
    """Names of the drop rules that apply to an item of this kind, in order."""
    return ((EMAIL_ONLY_RULES if kind in EMAIL_KINDS else ()) + UNIVERSAL_RULES
            + (CHAT_ONLY_RULES if kind in CHAT_KINDS else ()))


def check_headers(msg: Item, subject: str, config: DropConfig = DEFAULT_CONFIG,
                  kind: str = KIND_MESSAGE) -> Optional[str]:
    """All checks that don't need the body, in priority order. `subject` is the
    normalized subject (or title, for non-email items)."""
    for name in rules_for(kind):
        rule = _HEADER_RULES.get(name)
        reason = rule(msg, subject, config) if rule else None
        if reason:
            return reason
    return None
