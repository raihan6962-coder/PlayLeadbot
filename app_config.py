"""
config.py — Centralised runtime + env configuration management.
All modules pull settings through get_cfg() so there is a single
source of truth for every key.
"""

import os
import threading
from typing import Any

_run_cfg: dict = {}
_lock = threading.Lock()


def set_run_cfg(cfg: dict) -> None:
    """Overwrite run-time config (called at automation start)."""
    with _lock:
        _run_cfg.clear()
        _run_cfg.update(cfg)


def get_cfg(key: str, fallback: Any = "") -> Any:
    """Read from run-time config first, then environment."""
    with _lock:
        val = _run_cfg.get(key)
    if val is not None and val != "":
        return val
    return os.environ.get(key, fallback)


def get_bool(key: str, fallback: bool = False) -> bool:
    val = get_cfg(key, "")
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("1", "true", "yes", "on")


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(get_cfg(key, fallback))
    except (TypeError, ValueError):
        return fallback


def get_float(key: str, fallback: float = 0.0) -> float:
    try:
        return float(get_cfg(key, fallback))
    except (TypeError, ValueError):
        return fallback


# ── Allowed / blocked developer country codes ─────────────────────────────────
ALLOWED_COUNTRIES = {
    "US", "GB", "CA", "AU", "NZ", "DE", "FR", "NL", "SE", "NO",
    "DK", "FI", "CH", "AT", "BE", "IE", "SG", "JP", "KR", "IL",
    "IT", "ES", "PT", "PL", "CZ", "HU", "RO", "GR", "ZA", "AE",
    "SA", "QA", "KW", "BH", "MX", "BR", "AR", "CL", "CO",
}

BLOCKED_COUNTRIES = {
    "BD", "IN", "PK", "NG", "GH", "KE", "TZ", "UG", "ET", "EG",
    "MA", "TN", "DZ", "LY", "SD", "SO", "AO", "MZ", "ZM", "ZW",
    "MW", "RW", "SN", "CI", "CM", "CD", "MG", "MM", "KH", "LA",
    "NP", "LK", "AF", "IQ", "SY", "YE", "LB", "JO", "PS", "PH",
    "ID", "VN", "TH", "MY",
}

BLOCKED_CITY_KEYWORDS = [
    "bangladesh", "dhaka", "chittagong",
    "india", "mumbai", "delhi", "bangalore", "hyderabad", "chennai",
    "kolkata", "pune", "bengaluru",
    "pakistan", "karachi", "lahore", "islamabad",
    "nigeria", "lagos", "abuja",
    "kenya", "nairobi",
    "ghana", "accra",
    "indonesia", "jakarta",
    "philippines", "manila",
    "vietnam", "hanoi", "ho chi minh",
    "myanmar", "yangon",
    "cambodia", "phnom penh",
    "nepal", "kathmandu",
    "sri lanka", "colombo",
    "ethiopia", "addis ababa",
    "egypt", "cairo",
    "morocco", "casablanca",
    "tanzania", "dar es salaam",
    "uganda", "kampala",
    "malaysia", "kuala lumpur",
    "thailand", "bangkok",
]
