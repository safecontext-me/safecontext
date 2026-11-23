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

from .models import KIND_MESSAGE, RawItem, RawMessage
from .otp import looks_like_security_mail

SPAM_TRASH_LABELS = frozenset({"SPAM", "TRASH"})
CATEGORY_LABELS = {
    "CATEGORY_PROMOTIONS": "category:promotions",
    "CATEGORY_SOCIAL": "category:social",
    "CATEGORY_FORUMS": "category:forums",
}

_NO_REPLY_LOCAL = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon|postmaster|bounces?)(?:[-+_.].*)?$",
    re.I,
)

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
    if _NO_REPLY_LOCAL.match(_sender_local(msg.header("from"))):
        return "bulk:no_reply_sender"
    return None


def check_sensitive_sender(msg: RawMessage, config: DropConfig = DEFAULT_CONFIG) -> Optional[str]:
    domain = _sender_domain(msg.header("from"))
    if not domain or _domain_matches(domain, config.allow_domains):
        return None
    if _domain_matches(domain, config.sensitive_domains):
        return "sensitive:sender_domain"
    # Only look at the registrable part, not the TLD, for keyword tokens.
    labels = domain.split(".")[:-1]
    if any(_SENSITIVE_DOMAIN_TOKENS.search(label) for label in labels):
        return "sensitive:sender_keyword"
    return None


def check_subject(subject: str) -> Optional[str]:
    if _SECURITY_SUBJECT.search(subject):
        return "security:subject"
    if _SENSITIVE_SUBJECT.search(subject):
        return "sensitive:subject"
    return None


def check_security_body(body: str) -> Optional[str]:
    if looks_like_security_mail(body) or _PASSWORD_RESET_BODY.search(body):
        return "security:body"
    return None


# --- which rules apply to which kind of item --------------------------------
#
# Email-only rules read things only email has: Gmail labels (spam, drafts,
# Promotions…) and bulk-mail headers (List-Unsubscribe, Precedence, no-reply
# senders). A chat or a calendar invite never carries them, and a friend's
# text from a "noreply"-looking handle must not be dropped.
#
# Universal rules protect what the product exists to protect, so they apply to
# every source: security / one-time-code content, and banking, tax and health
# senders and subjects (titles). Inline redaction (codes, cards, accounts,
# addresses, …) also runs on every item; see pipeline.scrub_item.

EMAIL_ONLY_RULES: Tuple[str, ...] = ("labels", "bulk")
UNIVERSAL_RULES: Tuple[str, ...] = ("sensitive_sender", "subject", "security_body")
EMAIL_KINDS = frozenset({KIND_MESSAGE})

Item = Union[RawMessage, RawItem]
_HEADER_RULES: Dict[str, Callable[[Item, str, DropConfig], Optional[str]]] = {
    "labels": lambda m, subject, cfg: check_labels(m, cfg),
    "bulk": lambda m, subject, cfg: check_bulk(m, cfg),
    "sensitive_sender": lambda m, subject, cfg: check_sensitive_sender(m, cfg),
    "subject": lambda m, subject, cfg: check_subject(subject),
}


def rules_for(kind: str) -> Tuple[str, ...]:
    """Names of the drop rules that apply to an item of this kind, in order."""
    return (EMAIL_ONLY_RULES if kind in EMAIL_KINDS else ()) + UNIVERSAL_RULES


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
