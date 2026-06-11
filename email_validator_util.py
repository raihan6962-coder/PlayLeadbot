"""
email_validator_util.py
────────────────────────────────────────────────────────────────────────────
UPDATE #1 — Professional email validation before saving leads.

Performs:
  1. Syntax validation (RFC-ish regex)
  2. Domain validity (must resolve)
  3. MX record check (domain must accept mail)
  4. Disposable / temporary email domain detection
  5. Basic role-account / deliverability heuristic scoring

This module is fully self-contained and additive — it does NOT change any
existing scraping, lead-generation, or sheet logic. It is only called from
main.py right before a lead is appended/saved.

Returns a dict:
{
  "valid": bool,
  "reason": str,            # human readable reason (esp. when invalid)
  "syntax_ok": bool,
  "mx_found": bool,
  "disposable": bool,
  "domain": str,
  "deliverability_score": int   # 0-100 best-effort score
}
"""

import re
import socket
import functools

try:
    import dns.resolver
    _DNS_AVAILABLE = True
except Exception:
    _DNS_AVAILABLE = False

EMAIL_SYNTAX_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$"
)

# A solid, commonly-used list of disposable / temp-mail domains.
# Additive list — does not affect any existing functionality.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "tempmail.com", "temp-mail.org", "guerrillamail.com",
    "guerrillamail.net", "guerrillamail.org", "guerrillamailblock.com",
    "10minutemail.com", "10minutemail.net", "throwawaymail.com",
    "yopmail.com", "yopmail.net", "yopmail.fr", "trashmail.com",
    "trashmail.net", "getnada.com", "fakeinbox.com", "sharklasers.com",
    "dispostable.com", "mailnesia.com", "mintemail.com", "mailcatch.com",
    "mailnull.com", "spamgourmet.com", "spam4.me", "tempinbox.com",
    "moakt.com", "discardmail.com", "discardmail.de", "trbvm.com",
    "emailondeck.com", "33mail.com", "mytemp.email", "tempr.email",
    "burnermail.io", "mohmal.com", "throwam.com", "anonbox.net",
    "getairmail.com", "harakirimail.com", "jetable.org", "spambog.com",
    "mailsac.com", "tmpmail.org", "tmpmail.net", "tmpeml.com",
    "fakemail.net", "fakemailgenerator.com", "luxusmail.org",
    "incognitomail.com", "no-spam.ws", "notsharingmy.info",
    "mailtemp.io", "tempemail.co", "temp-mail.io", "emailfake.com",
}

# Generic / role-based local-parts that tend to have lower deliverability
# (often unmonitored) — used only for scoring, never for outright rejection.
ROLE_LOCAL_PARTS = {
    "admin", "support", "info", "contact", "sales", "noreply", "no-reply",
    "webmaster", "postmaster", "abuse", "help",
}


@functools.lru_cache(maxsize=2048)
def _has_mx_record(domain: str) -> bool:
    """Check whether the domain has MX (or fallback A) records."""
    if not _DNS_AVAILABLE:
        # dnspython not installed — fall back to a basic socket check
        # so the rest of the pipeline keeps working.
        try:
            socket.gethostbyname(domain)
            return True
        except Exception:
            return False

    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = 4
        resolver.lifetime = 4
        answers = resolver.resolve(domain, "MX")
        return len(answers) > 0
    except Exception:
        # Some domains accept mail without MX (fallback to A record)
        try:
            resolver = dns.resolver.Resolver()
            resolver.timeout = 4
            resolver.lifetime = 4
            a = resolver.resolve(domain, "A")
            return len(a) > 0
        except Exception:
            return False


def validate_email_full(email: str) -> dict:
    """
    Run the full validation pipeline on a single email address.
    Safe to call repeatedly — MX lookups are cached per-domain.
    """
    result = {
        "valid": False,
        "reason": "",
        "syntax_ok": False,
        "mx_found": False,
        "disposable": False,
        "domain": "",
        "deliverability_score": 0,
    }

    if not email or "@" not in email:
        result["reason"] = "empty or malformed email"
        return result

    email = email.strip()
    domain = email.rsplit("@", 1)[-1].lower().strip()
    local_part = email.rsplit("@", 1)[0].lower().strip()
    result["domain"] = domain

    # 1) Syntax check
    if not EMAIL_SYNTAX_RE.match(email):
        result["reason"] = "invalid syntax"
        return result
    result["syntax_ok"] = True

    # 2) Disposable domain check
    if domain in DISPOSABLE_DOMAINS:
        result["disposable"] = True
        result["reason"] = "disposable/temporary email domain"
        return result

    # 3) MX / domain validity check
    mx_ok = _has_mx_record(domain)
    result["mx_found"] = mx_ok
    if not mx_ok:
        result["reason"] = "domain has no MX/A records (undeliverable)"
        return result

    # 4) Deliverability scoring (best-effort heuristic)
    score = 100
    if local_part in ROLE_LOCAL_PARTS:
        score -= 15
    if len(local_part) <= 2:
        score -= 10
    if any(ch.isdigit() for ch in local_part) and sum(c.isdigit() for c in local_part) > 4:
        score -= 10
    result["deliverability_score"] = max(0, score)

    # All checks passed
    result["valid"] = True
    result["reason"] = "ok"
    return result


def is_email_valid(email: str) -> bool:
    """Convenience wrapper — returns just the boolean validity."""
    return validate_email_full(email).get("valid", False)
