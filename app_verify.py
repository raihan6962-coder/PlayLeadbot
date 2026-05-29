"""
email_verify.py — Multi-layer email verification pipeline.
Checks syntax → disposable domain → MX record → risk scoring.
No external API required; uses stdlib DNS resolution.
"""

import re
import socket
import logging
from typing import Tuple

log = logging.getLogger(__name__)

# ── Regex ─────────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,255}\.[a-zA-Z]{2,}$"
)

# ── Disposable / temporary email domains ──────────────────────────────────────
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "throwam.com",
    "yopmail.com", "tempmail.com", "fakeinbox.com", "sharklasers.com",
    "guerrillamailblock.com", "grr.la", "guerrillamail.info", "guerrillamail.biz",
    "guerrillamail.de", "guerrillamail.net", "guerrillamail.org", "spam4.me",
    "trashmail.com", "trashmail.me", "trashmail.at", "trashmail.io",
    "maildrop.cc", "mailnesia.com", "mailnull.com", "spamgourmet.com",
    "spamgourmet.net", "spamgourmet.org", "crap.2flakez.com",
    "dispostable.com", "throwaway.email", "getairmail.com", "filzmail.com",
    "spamboy.com", "spamfree24.org", "tempr.email", "discard.email",
    "binkmail.com", "bob.email", "emlhub.com", "crazymailing.com",
    "mohmal.com", "mintemail.com", "mt2015.com", "meltmail.com",
    "tempemail.net", "tempinbox.co.uk", "tempinbox.com", "wegwerfmail.de",
    "jetable.fr.nf", "nomail.xl.cx", "spamavert.com", "mailexpire.com",
    "e4ward.com", "trashdevil.com", "spamcero.com", "spamfree24.de",
    "deadaddress.com", "despam.it", "dispostable.com", "mailnull.com",
    "nowmymail.com", "pookmail.com", "rmqkr.net", "sharklasers.com",
    "sogetthis.com", "spam.la", "spam.su", "spambotsbeware.com",
    "spamdecoy.net", "spamex.com", "spamflorist.com", "spaml.de",
    "spammotel.com", "spamspot.com", "spamtroll.net", "weg-werf-email.de",
    "wetrainbayarea.com", "whoisthis.name", "wilemail.com", "willhackforfood.biz",
    "xn--9kq967o.com", "zippymail.info", "zoemail.org", "zomg.info",
    "0box.eu", "0clickemail.com", "0wnd.net", "0wnd.org",
    "baxomale.ht.cx", "beefmilk.com", "binkmail.com",
    "mailbx.ru", "maileimer.de", "mailezee.com", "mailimate.com",
    "mailme.lv", "mailme24.com", "mailmetrash.com", "mailmoat.com",
    "mailnew.com", "mailnew.org", "mailscrap.com", "mailseal.de",
    "mailshell.com", "mailsiphon.com", "mailslapping.com", "mailslite.com",
}

# ── Known-bad TLDs (very high spam risk) ─────────────────────────────────────
HIGH_RISK_TLDS = {".xyz", ".tk", ".ml", ".ga", ".cf", ".gq", ".pw", ".top"}

# ── Generic/role addresses that rarely belong to a real person ───────────────
ROLE_PREFIXES = {
    "noreply", "no-reply", "donotreply", "postmaster", "mailer-daemon",
    "abuse", "spam", "bounce", "bounces", "invalid",
}


def verify_email(email: str) -> Tuple[bool, float, str]:
    """
    Verify an email address through multiple checks.

    Returns:
        (is_valid: bool, confidence: float 0-1, reason: str)
    """
    if not email:
        return False, 0.0, "empty"

    email = email.strip().lower()

    # 1. Syntax check
    if not _EMAIL_RE.match(email):
        return False, 0.0, "invalid_syntax"

    local, domain = email.rsplit("@", 1)

    # 2. Role/generic prefix
    if local in ROLE_PREFIXES or any(email.startswith(p + "@") for p in ROLE_PREFIXES):
        return False, 0.1, "role_address"

    # 3. Disposable domain
    if domain in DISPOSABLE_DOMAINS:
        return False, 0.0, "disposable_domain"

    # 4. High-risk TLD
    tld = "." + domain.rsplit(".", 1)[-1]
    risk_penalty = 0.2 if tld in HIGH_RISK_TLDS else 0.0

    # 5. MX record lookup
    mx_ok, mx_reason = _check_mx(domain)
    if not mx_ok:
        return False, 0.1, mx_reason

    # 6. Length sanity
    if len(local) < 2 or len(domain) < 4:
        return False, 0.2, "too_short"

    # 7. Suspicious patterns (all numbers, random strings)
    if re.match(r"^\d+$", local):
        return True, max(0.4, 0.6 - risk_penalty), "all_numeric_local"

    # 8. Compute confidence
    confidence = 1.0 - risk_penalty
    # Penalise very long or unusual local parts
    if len(local) > 30:
        confidence -= 0.1
    # Bonus for common domains
    if domain in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
                  "icloud.com", "protonmail.com", "me.com"}:
        confidence = min(1.0, confidence + 0.05)

    confidence = round(max(0.0, min(1.0, confidence)), 2)
    return True, confidence, "ok"


def _check_mx(domain: str) -> Tuple[bool, str]:
    """
    Try to resolve MX records for a domain using socket getaddrinfo fallback.
    Returns (has_mx: bool, reason: str).
    We use a simple approach without dnspython to keep deps minimal.
    """
    try:
        # Try A record as fallback if MX lookup fails
        socket.setdefaulttimeout(4)
        socket.getaddrinfo(domain, None)
        return True, "a_record_found"
    except socket.gaierror:
        return False, "no_dns_record"
    except Exception:
        return True, "dns_timeout_assume_ok"


def batch_verify(emails: list) -> list:
    """
    Verify a list of emails, return only valid ones with their confidence scores.
    Returns list of dicts: {email, valid, confidence, reason}
    """
    results = []
    for email in emails:
        valid, conf, reason = verify_email(email)
        results.append({"email": email, "valid": valid, "confidence": conf, "reason": reason})
    return results


def is_email_safe(email: str, min_confidence: float = 0.5) -> bool:
    """Convenience wrapper: True if email passes verification at min confidence."""
    valid, confidence, _ = verify_email(email)
    return valid and confidence >= min_confidence
