"""
keyword_engine.py — AI keyword generation with category intent scoring.
Uses Groq to generate semantically relevant keywords, then scores each
for commercial intent and reputation-sensitivity before accepting it.
"""

import re
import json
import logging
from typing import List

from groq import Groq
from app_config import get_cfg

log = logging.getLogger(__name__)

# ── High-value app categories (depend on trust / social proof) ────────────────
HIGH_VALUE_CATEGORIES = {
    # Finance & Money
    "finance", "fintech", "trading", "investment", "stock", "crypto",
    "cryptocurrency", "bitcoin", "forex", "banking", "bank", "loan",
    "lending", "insurance", "insurtech", "mortgage", "wealth", "budget",
    "accounting", "invoice", "payment", "wallet", "remittance",
    # Health & Medical
    "telemedicine", "telehealth", "healthcare", "medical", "health",
    "mental health", "therapy", "doctor", "clinic", "pharmacy",
    "fitness", "nutrition", "diet", "wellness",
    # Legal & Professional
    "legal", "lawyer", "attorney", "compliance", "contract",
    "notary", "paralegal", "law",
    # Education & Learning
    "education", "edtech", "learning", "tutoring", "course", "language",
    "exam", "certification", "training", "university", "school",
    # Social & Relationships
    "dating", "relationship", "social", "networking", "matchmaking",
    # Marketplace & Commerce
    "marketplace", "ecommerce", "delivery", "logistics", "rental",
    "booking", "reservation", "shop", "store",
    # Security & Privacy
    "vpn", "security", "privacy", "password", "antivirus",
    "authentication", "2fa",
    # AI & SaaS tools
    "ai assistant", "productivity", "crm", "erp", "saas", "automation",
    "project management", "collaboration", "workflow",
    # Lifestyle with monetization
    "travel", "hotel", "flight", "real estate", "property",
    "job", "freelance", "gig", "career",
    # Entertainment with IAP
    "casino", "betting", "gambling", "lottery", "fantasy sports",
    "game subscription", "streaming",
}

# ── Low-value categories (skip these — no trust dependency) ──────────────────
LOW_VALUE_CATEGORIES = {
    "calculator", "flashlight", "wallpaper", "ringtone", "qr", "qr scanner",
    "barcode", "compass", "ruler", "unit converter", "unit conversion",
    "file manager", "folder manager", "cleaner", "junk cleaner", "booster",
    "battery saver", "screen recorder", "screen lock", "clock", "alarm",
    "stopwatch", "timer app", "weather widget", "notes", "notepad",
    "simple game", "puzzle", "coloring book", "drawing", "paint",
    "basic utility", "emoji keyboard", "sticker", "gif maker",
    "lock screen", "theme", "launcher", "icon pack",
}


def _score_keyword(kw: str) -> float:
    """
    Score keyword for commercial intent + reputation sensitivity (0–1).
    Higher = more likely to find app developers who need reputation services.
    """
    kw_lower = kw.lower()

    # Hard reject low-value
    for bad in LOW_VALUE_CATEGORIES:
        if bad in kw_lower:
            return 0.0

    score = 0.3  # baseline

    # High-value keyword match
    for good in HIGH_VALUE_CATEGORIES:
        if good in kw_lower:
            score += 0.5
            break

    # Length signal (longer = more specific niche = better)
    words = kw_lower.split()
    if len(words) >= 2:
        score += 0.1
    if len(words) >= 3:
        score += 0.05

    # Negative signals
    if any(w in kw_lower for w in ["free", "simple", "basic", "lite", "mini"]):
        score -= 0.1

    return round(min(1.0, max(0.0, score)), 2)


def _build_prompt(original: str, used: list) -> str:
    return f"""You are a senior Google Play Store keyword strategist specializing in app reputation and B2B outreach.

CONTEXT:
You are generating search keywords to find Android app developers who:
- Need reputation improvement (ratings, reviews)
- Work in competitive niches where trust and social proof DIRECTLY impact revenue
- Have real business monetization (subscriptions, in-app purchases, leads, bookings)

ORIGINAL SEED KEYWORD: "{original}"
ALREADY USED: {json.dumps(used if used else [])}

YOUR TASK:
Generate exactly 10 NEW search keywords.

STRICT RULES:
1. Only generate keywords for app categories where trust/ratings significantly impact user conversion:
   GOOD examples: insurance apps, trading platforms, dating apps, fintech tools, VPN services,
   telemedicine, legal services, loan apps, crypto wallets, edtech, SaaS tools, marketplaces,
   food delivery, HR software, mental health apps
2. NEVER generate keywords for: calculator, flashlight, wallpaper, file manager, QR scanner,
   alarm clock, unit converter, screen cleaner, lock screen, emoji, sticker, drawing, simple games
3. Each keyword should be 2-4 words long and highly specific to a monetized niche
4. Keywords should reflect what real developers search for when building these apps
5. Vary the angle: mix app type + platform context (e.g. "finance tracker app", "trading signal android")

Return ONLY a raw JSON array of strings. No explanation, no markdown, no extra text.
Example: ["crypto wallet android", "trading signals app", "loan management platform"]"""


def ai_gen_keywords(original: str, used: list) -> List[str]:
    """
    Generate context-aware, intent-scored keywords.
    Returns only keywords that pass the intent score threshold.
    """
    api_key = get_cfg("GROQ_API_KEY")
    if not api_key:
        log.warning("GROQ_API_KEY not set — skipping AI keyword generation")
        return []

    client = Groq(api_key=api_key)
    prompt = _build_prompt(original, used)

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.75,
            max_tokens=400,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()

        keywords: list = json.loads(raw)
        if not isinstance(keywords, list):
            return []

        # Score and filter
        accepted = []
        for kw in keywords:
            if not isinstance(kw, str):
                continue
            kw = kw.strip().lower()
            if kw in used:
                continue
            score = _score_keyword(kw)
            if score >= 0.3:
                accepted.append(kw)
                log.info(f"Keyword accepted [{score:.2f}]: {kw}")
            else:
                log.info(f"Keyword rejected [{score:.2f}]: {kw}")

        return accepted

    except (json.JSONDecodeError, Exception) as e:
        log.error(f"AI keyword generation error: {e}")
        return []


def score_keyword_public(kw: str) -> dict:
    """Public method returning score + verdict for dashboard display."""
    score = _score_keyword(kw)
    if score >= 0.7:
        verdict = "high_value"
    elif score >= 0.3:
        verdict = "medium_value"
    else:
        verdict = "low_value"
    return {"keyword": kw, "score": score, "verdict": verdict}
