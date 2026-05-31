"""
scraper.py — Google Play Store scraper with multi-selector rating extraction,
confidence scoring, Hunter/Normal mode enforcement, and country filtering.
"""

import re
import time
import logging
from typing import Optional, Tuple

import requests
from google_play_scraper import search, app as gp_app

from app_config import (
    get_cfg, ALLOWED_COUNTRIES, BLOCKED_COUNTRIES, BLOCKED_CITY_KEYWORDS
)
from app_state import (
    seen_id, mark_id, seen_email, mark_email,
    in_sheet, register_sheet, push_log
)
from app_verify import is_email_safe, verify_email as _verify_email_full_raw

def _verify_email_full(email: str):
    """Wrapper that returns (valid, confidence, reason) with proper logging."""
    try:
        return _verify_email_full_raw(email)
    except Exception as e:
        log.warning(f"Email verification error for {email}: {e}")
        return True, 0.5, "verify_error_assume_ok"
import app_sheets as sheet_manager

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

SEARCH_COMBOS = [
    ("en", "us"), ("en", "gb"), ("en", "au"), ("en", "ca"),
    ("en", "in"), ("en", "de"), ("en", "fr"), ("en", "nl"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Rating extraction with confidence scoring
# ─────────────────────────────────────────────────────────────────────────────

def extract_rating(details: dict) -> Tuple[Optional[float], int, float]:
    """
    Robustly extract rating data from google-play-scraper output.

    Returns:
        (score: float|None, ratings_count: int, confidence: float 0-1)

    Confidence levels:
        0.9+  → multiple signals confirm result
        0.7   → two signals agree
        0.5   → one signal, ambiguous
        0.0   → confirmed no ratings
    """
    score         = details.get("score")
    ratings_count = details.get("ratings")  or 0
    reviews_count = details.get("reviews")  or 0
    histogram     = details.get("histogram") or {}

    # Normalise: some locales return 0.0 instead of None for unrated apps
    if score == 0.0 and ratings_count == 0 and reviews_count == 0:
        score = None

    # Histogram confirmation — any star bucket with > 0 means rated
    hist_has_data = isinstance(histogram, dict) and any(
        (v or 0) > 0 for v in histogram.values()
    )

    # Build confidence signal list
    signals_rated = []
    if score is not None and score > 0:
        signals_rated.append("score")
    if ratings_count > 0:
        signals_rated.append("ratings_count")
    if reviews_count > 0:
        signals_rated.append("reviews_count")
    if hist_has_data:
        signals_rated.append("histogram")

    if not signals_rated:
        # All signals agree: no ratings
        return None, 0, 1.0

    # Resolve score when it's None/0 but we have ratings_count
    if (score is None or score == 0) and ratings_count > 0:
        # Can't determine score value with confidence
        score = 0.0   # Mark as present but unknown exact value
        signals_rated.append("inferred")

    confidence = min(1.0, 0.5 + len(signals_rated) * 0.15)

    # Extra validation: implausible score range
    if score is not None and not (0.0 <= score <= 5.0):
        score = None
        confidence *= 0.5

    return score, ratings_count, round(confidence, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Mode filter
# ─────────────────────────────────────────────────────────────────────────────

def passes_filter(installs: int, score: Optional[float],
                  ratings_count: int, confidence: float,
                  hunter: dict) -> Tuple[bool, str]:
    """
    Apply Hunter or Normal mode filter.

    Hunter mode targets apps with poor ratings:
      - App MUST have some ratings
      - installs <= max_installs (default 50,000 — wider net)
      - score <= max_score (default 3.0) — includes mediocre apps too

    Normal mode targets new/unrated apps:
      - App should have very few or no ratings (≤ 10 ratings)
      - installs <= 100,000 — much wider to get more leads
    """
    is_rated    = (score is not None) or (ratings_count > 0)
    few_ratings = ratings_count <= 10  # treat as essentially unrated

    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 50_000)
        max_score = float(hunter.get("max_score")  or 3.0)

        if not is_rated:
            return False, "hunter:no_ratings"
        if installs > max_inst:
            return False, f"hunter:too_many_installs({installs})"
        if score is not None and score > max_score:
            return False, f"hunter:score_too_high({score:.1f})"
        return True, "ok"

    # Normal mode — only accept apps with ZERO ratings (truly new apps)
    # This ensures we only contact developers who genuinely need our service
    if is_rated:
        return False, "normal:has_ratings"
    if installs > 50_000:
        return False, f"normal:too_many_installs({installs})"
    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Country filter
# ─────────────────────────────────────────────────────────────────────────────

def is_allowed_country(details: dict) -> bool:
    country_code = (
        details.get("developerCountry") or details.get("country") or ""
    ).upper().strip()

    if country_code:
        if country_code in BLOCKED_COUNTRIES:
            return False
        if country_code in ALLOWED_COUNTRIES:
            return True
        return True  # Unknown → allow

    # Fallback: check developer address
    dev_address = (details.get("developerAddress") or "").lower()
    if not dev_address:
        return True

    for kw in BLOCKED_CITY_KEYWORDS:
        if kw in dev_address:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Email extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_email(details: dict) -> str:
    """Extract best email from app details fields, in priority order."""
    sources = [
        details.get("developerEmail", ""),
        details.get("privacyPolicy", ""),
        details.get("description", ""),
        details.get("recentChanges", ""),
        details.get("developerWebsite", ""),
    ]
    for src in sources:
        if not src:
            continue
        m = _EMAIL_RE.search(str(src))
        if m:
            email = m.group(0).lower()
            # Skip generic/system emails
            if not email.startswith(("noreply", "no-reply", "donotreply")):
                return email
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Core scraper
# ─────────────────────────────────────────────────────────────────────────────

def scrape_keyword(keyword: str, hunter: dict, stop_event) -> list:
    """
    Scrape Play Store for a single keyword across multiple country combos.
    Returns a list of validated lead dicts.
    """
    mode_name = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🔍 [{mode_name}] Scraping: '{keyword}'")
    leads = []

    for lang, country in SEARCH_COMBOS:
        if stop_event.is_set():
            break

        # ── Search ────────────────────────────────────────────────────────────
        try:
            results = search(keyword, lang=lang, country=country, n_hits=30)
        except Exception as e:
            push_log(f"  ⚠️ Search error ({country}): {e}")
            time.sleep(2)
            continue

        for item in results:
            if stop_event.is_set():
                break

            app_id = item.get("appId", "")
            if not app_id:
                continue

            # In-memory dedup
            if seen_id(app_id):
                continue
            mark_id(app_id)

            # Cross-run sheet dedup
            if in_sheet(app_id, ""):
                push_log(f"  ⏭️  Skip (in sheet): {app_id}")
                continue

            # ── Fetch full details ─────────────────────────────────────────────
            try:
                details = gp_app(app_id, lang="en", country="us")
            except Exception:
                continue

            installs = details.get("minInstalls") or 0

            # ── Rating extraction with confidence ──────────────────────────────
            score, ratings_count, confidence = extract_rating(details)

            # ── Mode filter ────────────────────────────────────────────────────
            ok, reason = passes_filter(installs, score, ratings_count, confidence, hunter)
            if not ok:
                continue

            # ── Country filter ─────────────────────────────────────────────────
            if not is_allowed_country(details):
                push_log(f"  🚫 Skip (blocked country): {details.get('title', app_id)}")
                continue

            # ── Email extraction + verification ───────────────────────────────
            email = extract_email(details)
            if not email:
                continue

            # Verify email before accepting lead
            valid, conf, reason = _verify_email_full(email)
            if not valid:
                push_log(f"  ⚠️  Skip (email verify failed — {reason}): {email}")
                continue
            if conf < 0.5:
                push_log(f"  ⚠️  Skip (low email confidence {conf:.2f} — {reason}): {email}")
                continue

            # Email dedup
            if seen_email(email) or in_sheet("", email):
                push_log(f"  ⏭️  Skip (email dup): {email}")
                continue

            mark_email(email)
            register_sheet(app_id, email)

            # ── Build lead dict ───────────────────────────────────────────────
            score_str = f"{score:.1f}★" if (score and score > 0) else "new (no rating)"
            push_log(
                f"  ✅ {details.get('title', app_id)} | "
                f"{installs:,} installs | {score_str} | "
                f"{ratings_count} ratings | conf={confidence} | {email}"
            )

            lead = {
                "app_id":            app_id,
                "app_name":          details.get("title", ""),
                "developer":         details.get("developer", ""),
                "email":             email,
                "category":          details.get("genre", ""),
                "installs":          installs,
                "score":             score,
                "ratings_count":     ratings_count,
                "reviews_count":     details.get("reviews") or 0,
                "rating_confidence": confidence,
                "description":       (details.get("description") or "")[:300],
                "url":               f"https://play.google.com/store/apps/details?id={app_id}",
                "icon":              details.get("icon", ""),
                "keyword":           keyword,
                "mode":              mode_name.lower(),
                "scraped_at":        time.strftime("%Y-%m-%d %H:%M:%S"),
                "email_sent":        False,
            }
            leads.append(lead)

            time.sleep(0.3)

        push_log(f"  [{country}] done. Leads so far: {len(leads)}")
        time.sleep(0.6)

    push_log(f"  📦 {len(leads)} new leads from '{keyword}'")
    sheet_manager.sheet_log_keyword(keyword, len(leads), mode_name)
    return leads
