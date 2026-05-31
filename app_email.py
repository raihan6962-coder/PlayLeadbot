"""
email_engine.py — AI email rewriting, spam word filtering,
deliverability optimization, and human-like sending delays.
"""

import re
import json
import time
import random
import logging
from typing import Tuple, List

from groq import Groq
from app_config import get_cfg

log = logging.getLogger(__name__)

# ── Default templates ─────────────────────────────────────────────────────────
DEFAULT_SUBJECT = "Quick question about {{app_name}}"
DEFAULT_BODY = """Hi {{developer}} team,

I came across {{app_name}} on Google Play and wanted to reach out.

I noticed your app could benefit from a stronger review presence — which is something we specialize in helping developers with.

We work with app developers to professionally improve their Play Store reputation through authentic outreach and review management strategies.

Would you be open to a quick 10-minute call this week to see if we can help?

Best,
{{sender_name}}
{{sender_company}}"""

# ── Spam trigger words / phrases ──────────────────────────────────────────────
_DEFAULT_SPAM_WORDS = [
    "guaranteed", "100% free", "act now", "limited time offer",
    "click here", "buy now", "order now", "special promotion",
    "winner", "you won", "cash prize", "make money fast",
    "double your", "earn extra", "work from home", "no obligation",
    "risk free", "satisfaction guaranteed", "no credit check",
    "pre-approved", "lowest price", "best price",
    "congratulations", "dear friend", "free offer", "free trial",
    "increase sales", "increase traffic", "marketing solution",
    "weight loss", "lose weight", "miracle", "amazing offer",
]

# ── Greeting variations ───────────────────────────────────────────────────────
_GREETINGS = [
    "Hi {dev},",
    "Hello {dev},",
    "Hey {dev},",
    "Hi {dev} team,",
    "Hello {dev} team,",
    "Hi there,",
    "Good day {dev},",
]

# ── CTA variations ────────────────────────────────────────────────────────────
_CTAS = [
    "Would you be open to a quick 10-minute call this week?",
    "Do you have 15 minutes this week to connect?",
    "Would a brief call this week work for you?",
    "Are you available for a short chat this week?",
    "Could we schedule a quick 10-minute conversation?",
    "Would you be interested in a brief call to explore this?",
]

# ── Sign-off variations ───────────────────────────────────────────────────────
_SIGNOFFS = [
    "Best regards,", "Kind regards,", "Best,",
    "Warm regards,", "Thanks,", "Cheers,",
]


# ─────────────────────────────────────────────────────────────────────────────
# Template filling
# ─────────────────────────────────────────────────────────────────────────────

def fill_template(tpl: str, lead: dict) -> str:
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    score_val      = lead.get("score")
    score_str      = f"{score_val:.1f} stars" if score_val else "no ratings yet"
    installs       = lead.get("installs", 0)
    install_str    = f"{installs:,}" if installs else "just launched"

    return (tpl
        .replace("{{app_name}}",       lead.get("app_name", ""))
        .replace("{{developer}}",      lead.get("developer", ""))
        .replace("{{category}}",       lead.get("category", ""))
        .replace("{{installs}}",       install_str)
        .replace("{{score}}",          score_str)
        .replace("{{url}}",            lead.get("url", ""))
        .replace("{{sender_name}}",    sender_name)
        .replace("{{sender_company}}", sender_company)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Spam word filter
# ─────────────────────────────────────────────────────────────────────────────

def _get_spam_words() -> List[str]:
    custom = get_cfg("SPAM_WORDS", "")
    extras = [w.strip().lower() for w in custom.replace(",", "\n").splitlines() if w.strip()]
    return _DEFAULT_SPAM_WORDS + extras


def detect_spam_words(text: str) -> List[str]:
    """Return list of found spam trigger words/phrases in text."""
    text_lower = text.lower()
    found = []
    for phrase in _get_spam_words():
        if phrase.lower() in text_lower:
            found.append(phrase)
    return found


def spam_score(subject: str, body: str) -> dict:
    """Compute a basic spam risk score (0–100, lower is better)."""
    combined = (subject + " " + body).lower()
    triggers = detect_spam_words(combined)
    score    = min(100, len(triggers) * 12)

    # Extra penalties
    caps_ratio = sum(1 for c in combined if c.isupper()) / max(len(combined), 1)
    if caps_ratio > 0.15:
        score += 15
    if "!!!" in combined or "???" in combined:
        score += 10
    if subject.isupper():
        score += 20

    return {
        "score":    min(100, score),
        "triggers": triggers,
        "verdict":  "clean" if score < 20 else ("risky" if score < 50 else "spam"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AI email rewriting
# ─────────────────────────────────────────────────────────────────────────────

def _build_rewrite_prompt(lead: dict, base_subject: str, base_body: str,
                           spam_words: List[str]) -> str:
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    score_val      = lead.get("score")
    score_info     = f"{score_val:.1f} stars" if score_val else "no ratings yet (brand new app)"
    installs       = lead.get("installs", 0)
    install_info   = f"{installs:,} installs" if installs else "just launched"

    spam_note = ""
    if spam_words:
        spam_note = (
            f"\nSPAM WORDS TO AVOID IN YOUR OUTPUT: {', '.join(spam_words)}\n"
            "Replace them with natural professional equivalents.\n"
        )

    return f"""You are an expert B2B cold email specialist who writes like a real human professional.

BASE TEMPLATE:
Subject: {base_subject}
Body:
{base_body}

APP DETAILS:
- App Name: {lead.get('app_name', '')}
- Developer: {lead.get('developer', '')}
- Category: {lead.get('category', '')}
- Installs: {install_info}
- Rating: {score_info}
- Play Store URL: {lead.get('url', '')}

SENDER:
- Name: {sender_name}
- Company: {sender_company}
{spam_note}
REWRITING RULES:
1. Fill in all template placeholders with the real app details above
2. Vary the sentence structures slightly — do not copy verbatim
3. Keep the SAME approximate length and number of paragraphs
4. Keep the same tone and message intent
5. Change 3-5 words or phrases naturally so each email feels fresh
6. Use a natural professional greeting and friendly sign-off
7. NEVER use spam trigger phrases, excessive punctuation, or ALL CAPS
8. Preserve all line breaks — each paragraph must remain separate
9. The final email should sound like it was personally written by a real human

Return ONLY valid JSON with exactly two keys, no markdown:
{{"subject": "...", "body": "..."}}"""


def _strip_spam_words(text: str) -> str:
    """Replace spam trigger words with neutral alternatives in-place."""
    REPLACEMENTS = {
        "guaranteed": "proven",
        "100% free": "completely free to try",
        "act now": "take a moment",
        "limited time offer": "special opportunity",
        "click here": "see more",
        "buy now": "get started",
        "order now": "begin today",
        "special promotion": "opportunity",
        "make money fast": "grow revenue",
        "double your": "significantly increase your",
        "earn extra": "generate additional",
        "work from home": "remote work",
        "no obligation": "no commitment required",
        "risk free": "risk-free trial",
        "satisfaction guaranteed": "results-focused",
        "congratulations": "great news",
        "dear friend": "hello",
        "free offer": "no-cost option",
        "free trial": "trial period",
        "increase sales": "grow your user base",
        "increase traffic": "boost visibility",
        "marketing solution": "growth strategy",
        "amazing offer": "great opportunity",
    }
    result = text
    for spam, safe in REPLACEMENTS.items():
        result = re.sub(re.escape(spam), safe, result, flags=re.IGNORECASE)
    # Also replace custom spam words from config with empty/neutral
    custom = get_cfg("SPAM_WORDS", "")
    custom_words = [w.strip() for w in custom.replace(",", "\n").splitlines() if w.strip()]
    for cw in custom_words:
        result = re.sub(re.escape(cw), "", result, flags=re.IGNORECASE)
    return result


def ai_rewrite_email(lead: dict, base_subject: str, base_body: str) -> Tuple[str, str]:
    """
    Produce a humanized, personalized version of the email.
    1. Fill template placeholders with lead data.
    2. Strip/replace all spam trigger words.
    3. Use Groq AI to further humanize (if API key is set).
    Falls back gracefully at each step.
    """
    api_key = get_cfg("GROQ_API_KEY")

    # Always do template fill + spam word stripping first
    filled_subject = _strip_spam_words(fill_template(base_subject, lead))
    filled_body    = _strip_spam_words(fill_template(base_body,    lead))

    if not api_key:
        return filled_subject, filled_body

    # Spam words already stripped above; just detect any remaining for AI prompt
    found_spam = detect_spam_words(filled_subject + " " + filled_body)

    client = Groq(api_key=api_key)
    prompt = _build_rewrite_prompt(lead, base_subject, base_body, found_spam)

    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.45 + (attempt * 0.1),
                max_tokens=600,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
            data = json.loads(raw)
            subject = (data.get("subject") or filled_subject).strip()
            body    = (data.get("body")    or filled_body).strip()
            body    = body.replace("\\n", "\n")

            # Post-send spam check
            check = spam_score(subject, body)
            if check["score"] >= 60 and attempt == 0:
                log.warning(f"  Email spam score {check['score']} — retrying rewrite")
                continue

            log.info(f"  AI email spam score: {check['score']} ({check['verdict']})")
            return subject, body

        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"  AI rewrite error (attempt {attempt + 1}): {e}")

    # Fallback — return spam-stripped template versions
    return filled_subject, filled_body


# ─────────────────────────────────────────────────────────────────────────────
# Humanized sending delays
# ─────────────────────────────────────────────────────────────────────────────

_DELAY_PROFILES = {
    "conservative": (90, 180),   # 1.5–3 min
    "normal":       (45, 120),   # 45 sec–2 min
    "aggressive":   (20, 60),    # 20 sec–1 min
}


def humanized_delay(stop_event, profile: str = "normal",
                    lead_idx: int = 0, total: int = 1) -> None:
    """
    Sleep for a randomized, human-like interval between emails.
    Uses Poisson-inspired jitter to avoid robotic patterns.
    """
    lo, hi = _DELAY_PROFILES.get(profile, (45, 120))

    # Occasionally take a longer "thinking break"
    if random.random() < 0.12:
        lo, hi = hi, hi * 2

    wait = random.uniform(lo, hi)
    # Add micro-jitter (±10%)
    wait += random.gauss(0, wait * 0.1)
    wait = max(lo * 0.8, wait)

    log.info(f"  ⏳ Humanized delay: {wait:.0f}s ({lead_idx + 1}/{total})")
    for _ in range(int(wait)):
        if stop_event.is_set():
            break
        time.sleep(1)


def get_send_profile() -> str:
    """Determine send profile from config."""
    raw = get_cfg("SEND_PROFILE", "normal").lower()
    return raw if raw in _DELAY_PROFILES else "normal"
