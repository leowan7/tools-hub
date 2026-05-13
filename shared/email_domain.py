"""Email-domain classification for signup filtering.

The Ranomics tools-hub serves protein engineers from three audiences:

  business    Industry researcher with a corporate email
              (e.g. user@biotechco.com).
  academic    Academic researcher with an institutional email
              (e.g. user@stanford.edu, user@cam.ac.uk).
  personal    Independent / hobbyist / student with a free-mail
              provider (e.g. user@gmail.com). These users are
              welcome — but the signup route requires a short
              "what are you working on" note to filter out
              spam-as-junk.

Disposable / throwaway addresses (mailinator.com, 10minutemail.com, etc.)
are hard-blocked: they're nearly always bot signups burning Resend +
Supabase resources with zero conversion path.

Usage
-----
    from shared.email_domain import classify_email, EmailClass

    cls = classify_email("alice@stanford.edu")
    # → EmailClass.ACADEMIC

The classifier is pure (no network calls, no DB) and safe to call from
request handlers. The disposable list lives in
``shared/disposable_domains.txt`` and is loaded once at module import.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class EmailClass(str, Enum):
    """Classification buckets for an inbound signup email."""

    DISPOSABLE = "disposable"
    PERSONAL = "personal"
    ACADEMIC = "academic"
    BUSINESS = "business"
    INVALID = "invalid"


# ---------------------------------------------------------------------------
# Personal-provider denylist
# ---------------------------------------------------------------------------
# Curated list of the most common free-mail providers. These addresses
# are still allowed to sign up — they just have to attach a short
# "what are you working on" note. The list is small on purpose: each
# entry is a popular brand consumer-grade provider, not a niche or
# corporate domain. Niche/personal-but-business-looking domains
# (e.g. "leowan.dev") classify as "business" by default.

PERSONAL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "yahoo.co.in",
    "yahoo.com.au",
    "yahoo.ca",
    "yahoo.fr",
    "yahoo.de",
    "ymail.com",
    "rocketmail.com",
    "outlook.com",
    "outlook.co.uk",
    "hotmail.com",
    "hotmail.co.uk",
    "hotmail.fr",
    "hotmail.de",
    "live.com",
    "live.co.uk",
    "msn.com",
    "aol.com",
    "aim.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "optonline.net",
    "comcast.net",
    "verizon.net",
    "att.net",
    "sbcglobal.net",
    "bellsouth.net",
    "cox.net",
    "charter.net",
    "earthlink.net",
    "juno.com",
    "netzero.net",
    "netzero.com",
    "mail.com",
    "protonmail.com",
    "proton.me",
    "pm.me",
    "tutanota.com",
    "tuta.io",
    "gmx.com",
    "gmx.us",
    "gmx.net",
    "gmx.de",
    "gmx.co.uk",
    "yandex.com",
    "yandex.ru",
    "mail.ru",
    "qq.com",
    "163.com",
    "126.com",
    "sina.com",
    "naver.com",
    "rediffmail.com",
    "fastmail.com",
    "fastmail.fm",
    "zoho.com",
    "duck.com",
    "hey.com",
})


# ---------------------------------------------------------------------------
# Academic suffix patterns
# ---------------------------------------------------------------------------
# Any domain ending in one of these suffixes classifies as academic.
# (Suffix match, not exact match, so "med.stanford.edu" still hits.)
# Order doesn't matter; we check all suffixes.

ACADEMIC_SUFFIXES: tuple[str, ...] = (
    ".edu",
    ".edu.au",
    ".edu.cn",
    ".edu.hk",
    ".edu.sg",
    ".edu.tw",
    ".edu.in",
    ".edu.pk",
    ".edu.ph",
    ".edu.mx",
    ".ac.uk",
    ".ac.jp",
    ".ac.nz",
    ".ac.za",
    ".ac.kr",
    ".ac.il",
    ".ac.at",
    ".ac.be",
    ".ac.cn",
    ".ac.in",
    ".ac.ir",
    ".ac.id",
    ".ac.th",
    ".uni-",
    ".university",
    ".college",
    ".institute.edu",
    ".harvard.edu",
    ".mit.edu",
)


# ---------------------------------------------------------------------------
# Disposable domain set (loaded from disk on import)
# ---------------------------------------------------------------------------

_DISPOSABLE_PATH = Path(__file__).with_name("disposable_domains.txt")


def _load_disposable_domains() -> frozenset[str]:
    try:
        text = _DISPOSABLE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "Disposable domains file missing at %s — disposable filter "
            "will pass everything through.",
            _DISPOSABLE_PATH,
        )
        return frozenset()
    out: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip().lower()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return frozenset(out)


DISPOSABLE_DOMAINS: frozenset[str] = _load_disposable_domains()


# ---------------------------------------------------------------------------
# RFC 5322-lite address pattern. Not a full validator — we only need to
# reject obvious junk before we trust the local-part / domain split.
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+$"
)


def classify_email(email: str | None) -> EmailClass:
    """Classify ``email`` into one of the ``EmailClass`` buckets.

    Returns ``EmailClass.INVALID`` for None, empty, malformed, or
    address strings that fail the lite regex. Otherwise returns the
    first matching bucket in this order:

        DISPOSABLE  →  ACADEMIC  →  PERSONAL  →  BUSINESS

    The order matters: a domain like "alumni.harvard.edu" should
    classify as academic even though the local part contains "alumni";
    a disposable address that *happens* to end in ".edu" still wins
    as disposable (which would be an unusual entry in our list, but
    we are conservative on the disposable side).
    """
    if not email or not isinstance(email, str):
        return EmailClass.INVALID
    cleaned = email.strip().lower()
    if not _EMAIL_RE.match(cleaned):
        return EmailClass.INVALID

    try:
        _, domain = cleaned.rsplit("@", 1)
    except ValueError:
        return EmailClass.INVALID
    if not domain:
        return EmailClass.INVALID

    if domain in DISPOSABLE_DOMAINS:
        return EmailClass.DISPOSABLE

    for suffix in ACADEMIC_SUFFIXES:
        if domain.endswith(suffix):
            return EmailClass.ACADEMIC

    if domain in PERSONAL_DOMAINS:
        return EmailClass.PERSONAL

    return EmailClass.BUSINESS


# ---------------------------------------------------------------------------
# Convenience helpers used by the daily digest / admin views
# ---------------------------------------------------------------------------


def signup_quality_for(classification: EmailClass, purpose: str | None) -> str:
    """Map a classification + purpose-text to the persisted quality tag.

    Stored on ``public.user_profiles.signup_quality``. The values are
    intentionally distinct from EmailClass: ``personal_explained`` is
    the joint state of (personal-domain + non-empty purpose), which
    the daily-digest "Who's hot" section uses to weight engagement
    signals.
    """
    if classification == EmailClass.ACADEMIC:
        return "academic"
    if classification == EmailClass.PERSONAL and purpose:
        return "personal_explained"
    return "business"
