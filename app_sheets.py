"""
app_sheets.py — Google Apps Script integration (single sheet URL).
All lead storage and retrieval operations route through one Code.gs deployment.
"""

import logging
from typing import Optional
import time
import requests

from app_config import get_cfg

log = logging.getLogger(__name__)


def _get_sheet_url() -> str:
    """Return the configured sheet Web App URL."""
    return (get_cfg("APPS_SCRIPT_WEB_URL") or "").strip()


def sheet_post(payload: dict, retries: int = 2) -> Optional[dict]:
    """POST payload to the Apps Script Web App URL. Retries on timeout."""
    url = _get_sheet_url()
    if not url:
        log.error("APPS_SCRIPT_WEB_URL not configured.")
        return None
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=20)
            return r.json() if r.text else {}
        except Exception as e:
            log.warning(f"Sheet POST error (attempt {attempt+1}): {e}")
    return None


# ── Specific sheet operations ─────────────────────────────────────────────────

def sheet_append_lead(lead: dict) -> None:
    sheet_post({"action": "append", "tab": "All Leads", "row": {
        "App Name":          lead["app_name"],
        "Developer":         lead["developer"],
        "Email":             lead["email"],
        "Category":          lead["category"],
        "Installs":          lead["installs"],
        "Score":             lead.get("score") or "",
        "Ratings":           lead.get("ratings_count") or 0,
        "URL":               lead["url"],
        "Keyword":           lead["keyword"],
        "Mode":              lead.get("mode", "normal"),
        "Scraped At":        lead["scraped_at"],
        "Email Sent":        "No",
        "App ID":            lead["app_id"],
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
        "Keyword":     keyword,
        "Leads Found": count,
        "Mode":        mode,
        "Logged At":   time.strftime("%Y-%m-%d %H:%M:%S"),
    }})


def sheet_load_memory() -> tuple:
    """Fetch all existing App IDs and Emails from the sheet for dedup."""
    result = sheet_post({"action": "get_all", "tab": "All Leads"})
    if not result:
        return set(), set()
    records = result.get("records", [])
    ids, emails = set(), set()
    for rec in records:
        aid   = (rec.get("App ID") or "").strip()
        email = (rec.get("Email")  or "").strip().lower()
        if aid:   ids.add(aid)
        if email: emails.add(email)
    return ids, emails


def sheet_fetch_pending() -> list:
    """Fetch leads where Email Sent = Pending from the sheet."""
    result = sheet_post({"action": "get_pending"})
    if not result:
        return []
    return result.get("leads", [])
