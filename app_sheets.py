"""
sheet_manager.py — Google Apps Script integration with multi-URL failover.
Supports multiple Web App URLs; automatically rotates when one hits quota.
"""

import time
import logging
import threading
from typing import Optional

import requests

from app_config import get_cfg

log = logging.getLogger(__name__)

# ── URL pool state ─────────────────────────────────────────────────────────────
_url_lock      = threading.Lock()
_url_pool:     list  = []          # list of {"url": str, "fails": int, "cooldown_until": float}
_current_idx:  int   = 0
_MAX_FAILS     = 3                 # consecutive failures before cooldown
_COOLDOWN_SEC  = 300               # 5-minute cooldown per URL


def _parse_urls(raw: str) -> list:
    """Parse newline/comma-separated Web App URLs."""
    urls = []
    for part in raw.replace(",", "\n").splitlines():
        u = part.strip()
        if u.startswith("http"):
            urls.append({"url": u, "fails": 0, "cooldown_until": 0.0})
    return urls


def _refresh_pool() -> None:
    """Reload URL pool from current config."""
    global _url_pool, _current_idx
    raw = get_cfg("APPS_SCRIPT_WEB_URLS") or get_cfg("APPS_SCRIPT_WEB_URL") or ""
    urls = _parse_urls(raw)
    with _url_lock:
        _url_pool    = urls
        _current_idx = 0


def _get_active_url() -> Optional[str]:
    """Return the next healthy URL, cycling through the pool."""
    global _current_idx
    with _url_lock:
        if not _url_pool:
            return None
        now = time.time()
        # Try each URL in round-robin; skip cooled-down ones
        for _ in range(len(_url_pool)):
            entry = _url_pool[_current_idx % len(_url_pool)]
            _current_idx += 1
            if entry["cooldown_until"] <= now:
                return entry["url"]
        return None


def _mark_fail(url: str) -> None:
    with _url_lock:
        for entry in _url_pool:
            if entry["url"] == url:
                entry["fails"] += 1
                if entry["fails"] >= _MAX_FAILS:
                    entry["cooldown_until"] = time.time() + _COOLDOWN_SEC
                    log.warning(f"URL entering cooldown: {url[:60]}…")
                break


def _mark_ok(url: str) -> None:
    with _url_lock:
        for entry in _url_pool:
            if entry["url"] == url:
                entry["fails"] = 0
                break


# ── Public API ────────────────────────────────────────────────────────────────

def sheet_post(payload: dict, retries: int = 2) -> Optional[dict]:
    """
    POST payload to the next healthy Apps Script URL.
    Automatically retries with the next URL on failure.
    Returns parsed JSON or None on total failure.
    """
    _refresh_pool()
    for attempt in range(max(1, len(_url_pool)) * retries):
        url = _get_active_url()
        if not url:
            log.error("No healthy Apps Script URL available.")
            return None
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 429:                     # quota exceeded
                log.warning(f"Quota exceeded on {url[:50]}… — rotating")
                _mark_fail(url)
                continue
            result = r.json() if r.text else {}
            _mark_ok(url)
            return result
        except Exception as e:
            log.warning(f"Sheet POST error ({url[:50]}…): {e}")
            _mark_fail(url)
    return None


def url_pool_status() -> list:
    """Return pool health info for the dashboard."""
    _refresh_pool()
    with _url_lock:
        now = time.time()
        return [
            {
                "url":       e["url"][:70] + ("…" if len(e["url"]) > 70 else ""),
                "fails":     e["fails"],
                "cooldown":  max(0, int(e["cooldown_until"] - now)),
                "healthy":   e["cooldown_until"] <= now,
            }
            for e in _url_pool
        ]


# ── Specific sheet operations ─────────────────────────────────────────────────

def sheet_append_lead(lead: dict) -> None:
    sheet_post({"action": "append", "tab": "All Leads", "row": {
        "App Name":   lead["app_name"],
        "Developer":  lead["developer"],
        "Email":      lead["email"],
        "Category":   lead["category"],
        "Installs":   lead["installs"],
        "Score":      lead.get("score") or "",
        "Ratings":    lead.get("ratings_count") or 0,
        "URL":        lead["url"],
        "Keyword":    lead["keyword"],
        "Mode":       lead.get("mode", "normal"),
        "Scraped At": lead["scraped_at"],
        "Email Sent": "No",
        "App ID":     lead["app_id"],
        "Rating Confidence": round(lead.get("rating_confidence", 0), 2),
    }})


def sheet_append_qualified(lead: dict) -> None:
    sheet_post({"action": "append", "tab": "Qualified Leads", "row": {
        "App Name":   lead["app_name"],
        "Developer":  lead["developer"],
        "Email":      lead["email"],
        "Category":   lead["category"],
        "Installs":   lead["installs"],
        "Score":      lead.get("score") or "",
        "URL":        lead["url"],
        "Keyword":    lead["keyword"],
        "Mode":       lead.get("mode", "normal"),
        "Scraped At": lead["scraped_at"],
        "Email Sent": "Pending",
        "App ID":     lead["app_id"],
    }})


def sheet_mark_sent(app_id: str, email: str, app_name: str) -> None:
    sheet_post({"action": "mark_sent", "app_id": app_id})
    sheet_post({"action": "append", "tab": "Email Sent", "row": {
        "App ID":   app_id,
        "App Name": app_name,
        "Email":    email,
        "Sent At":  time.strftime("%Y-%m-%d %H:%M:%S"),
    }})


def sheet_log_keyword(keyword: str, count: int, mode: str) -> None:
    sheet_post({"action": "append", "tab": "Keyword Log", "row": {
        "Keyword":    keyword,
        "Leads Found": count,
        "Mode":       mode,
        "Logged At":  time.strftime("%Y-%m-%d %H:%M:%S"),
    }})


def sheet_load_memory() -> tuple:
    """
    Fetch all existing App IDs and Emails from the sheet.
    Returns (ids: set, emails: set).
    """
    result = sheet_post({"action": "get_all", "tab": "All Leads"})
    if not result:
        return set(), set()
    records = result.get("records", [])
    ids    = set()
    emails = set()
    for rec in records:
        aid   = (rec.get("App ID") or "").strip()
        email = (rec.get("Email")  or "").strip().lower()
        if aid:
            ids.add(aid)
        if email:
            emails.add(email)
    return ids, emails


def sheet_fetch_pending() -> list:
    """Fetch leads where Email Sent = Pending (or No) from the sheet."""
    result = sheet_post({"action": "get_pending"})
    if not result:
        return []
    return result.get("leads", [])
