"""
state_manager.py — Thread-safe shared state for the automation engine.
All reads/writes go through the helper functions below.
"""

import threading
import time
import logging

log = logging.getLogger(__name__)

_lock = threading.Lock()

_state: dict = {
    "running":       False,
    "phase":         "idle",       # idle | loading_sheet | scraping | emailing | stopped | done
    "keyword":       "",
    "mode":          "normal",     # normal | hunter
    "keywords_used": [],
    "leads_found":   0,
    "emails_sent":   0,
    "emails_failed": 0,
    "leads":         [],
    "logs":          [],
    # CRM analytics accumulated during runs
    "crm": {
        "total_sent":    0,
        "total_failed":  0,
        "total_bounced": 0,
        "total_skipped": 0,
        "campaigns":     [],   # list of {keyword, leads, sent, failed, ts}
    },
}

stop_event = threading.Event()


# ── Dedup trackers ─────────────────────────────────────────────────────────────
_global_seen_ids:    set = set()
_global_seen_emails: set = set()
_seen_lock = threading.Lock()

# Sheet memory (loaded once per run from Google Sheets)
_sheet_ids:    set = set()
_sheet_emails: set = set()
_sheet_loaded: bool = False
_smem_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# State helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_state() -> dict:
    with _lock:
        return dict(_state)


def upd(**kw) -> None:
    with _lock:
        _state.update(kw)


def push_log(msg: str) -> None:
    entry = {"time": time.strftime("%H:%M:%S"), "msg": msg}
    with _lock:
        _state["logs"].append(entry)
        if len(_state["logs"]) > 600:
            _state["logs"] = _state["logs"][-600:]
    log.info(msg)


def inc_sent() -> None:
    with _lock:
        _state["emails_sent"] = _state.get("emails_sent", 0) + 1
        _state["crm"]["total_sent"] = _state["crm"].get("total_sent", 0) + 1


def inc_failed() -> None:
    with _lock:
        _state["emails_failed"] = _state.get("emails_failed", 0) + 1
        _state["crm"]["total_failed"] = _state["crm"].get("total_failed", 0) + 1


def reset_state(keep_crm: bool = True) -> None:
    with _lock:
        crm = dict(_state["crm"]) if keep_crm else {
            "total_sent": 0, "total_failed": 0, "total_bounced": 0,
            "total_skipped": 0, "campaigns": [],
        }
        _state.update({
            "running": False, "phase": "idle", "keyword": "",
            "mode": "normal", "keywords_used": [], "leads_found": 0,
            "emails_sent": 0, "emails_failed": 0, "leads": [], "logs": [],
            "crm": crm,
        })


# ─────────────────────────────────────────────────────────────────────────────
# In-memory dedup
# ─────────────────────────────────────────────────────────────────────────────

def seen_id(app_id: str) -> bool:
    with _seen_lock:
        return app_id in _global_seen_ids


def mark_id(app_id: str) -> None:
    with _seen_lock:
        _global_seen_ids.add(app_id)


def seen_email(email: str) -> bool:
    with _seen_lock:
        return email.lower() in _global_seen_emails


def mark_email(email: str) -> None:
    with _seen_lock:
        _global_seen_emails.add(email.lower())


def clear_dedup() -> None:
    global _global_seen_ids, _global_seen_emails
    with _seen_lock:
        _global_seen_ids = set()
        _global_seen_emails = set()


# ─────────────────────────────────────────────────────────────────────────────
# Sheet memory
# ─────────────────────────────────────────────────────────────────────────────

def load_sheet_memory(ids: set, emails: set) -> None:
    global _sheet_ids, _sheet_emails, _sheet_loaded
    with _smem_lock:
        _sheet_ids    = set(ids)
        _sheet_emails = {e.lower() for e in emails}
        _sheet_loaded = True


def in_sheet(app_id: str, email: str) -> bool:
    with _smem_lock:
        if app_id and app_id in _sheet_ids:
            return True
        if email and email.lower() in _sheet_emails:
            return True
    return False


def register_sheet(app_id: str, email: str) -> None:
    with _smem_lock:
        if app_id:
            _sheet_ids.add(app_id)
        if email:
            _sheet_emails.add(email.lower())


def clear_sheet_memory() -> None:
    global _sheet_ids, _sheet_emails, _sheet_loaded
    with _smem_lock:
        _sheet_ids    = set()
        _sheet_emails = set()
        _sheet_loaded = False


def sheet_memory_stats() -> dict:
    with _smem_lock:
        return {
            "loaded":       _sheet_loaded,
            "ids_count":    len(_sheet_ids),
            "emails_count": len(_sheet_emails),
        }
