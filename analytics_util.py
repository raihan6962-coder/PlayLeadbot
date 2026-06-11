"""
analytics_util.py
────────────────────────────────────────────────────────────────────────────
UPDATE #3 — Advanced analytics
UPDATE #4 — Advanced filtering

This module is purely additive: it does not touch lead generation, scraping,
or email sending logic. It provides:

  - EVENT TYPES logged to a new "Analytics" Sheet tab (UPDATE #5) every time
    something analytics-relevant happens (lead found, email sent, bounced,
    skipped, invalid email, etc.)

  - aggregate_analytics(records, filters) — takes the raw rows returned from
    the "Analytics" sheet tab (via the existing generic {"action":"get_all"}
    Apps Script endpoint) and computes every metric required by UPDATE #3,
    with the filters required by UPDATE #4 applied first.
"""

import time

# ── Event type constants (used as the "Event" column value in the Analytics tab)
EV_LEAD_GENERATED   = "lead_generated"
EV_QUALIFIED        = "qualified_lead"
EV_VALID_EMAIL      = "valid_email"
EV_INVALID_EMAIL    = "invalid_email"
EV_SKIPPED_LEAD     = "skipped_lead"
EV_EMAIL_SENT       = "email_sent"
EV_EMAIL_DELIVERED  = "email_delivered"
EV_EMAIL_FAILED     = "email_failed"
EV_EMAIL_BOUNCED    = "email_bounced"
EV_EMAIL_OPENED     = "email_opened"
EV_EMAIL_REPLIED    = "email_replied"
EV_EMAIL_CLICKED    = "email_clicked"
EV_UNSUBSCRIBED     = "unsubscribed"
EV_PENDING_EMAIL    = "pending_email"
EV_CAMPAIGN_START   = "campaign_started"
EV_CAMPAIGN_COMPLETE= "campaign_completed"


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def build_analytics_row(event: str, **kw) -> dict:
    """
    Build a row dict for the 'Analytics' sheet tab.
    Extra keys (campaign/keyword, lead_source, industry, country, app_id,
    email, status flags) are stored so the dashboard can filter/sort on them.
    """
    row = {
        "Timestamp": now_str(),
        "Date": time.strftime("%Y-%m-%d"),
        "Event": event,
        "Campaign": kw.get("campaign", ""),
        "Keyword": kw.get("keyword", ""),
        "Lead Source": kw.get("lead_source", "Play Store"),
        "Industry": kw.get("industry", kw.get("category", "")),
        "Country": kw.get("country", ""),
        "App ID": kw.get("app_id", ""),
        "App Name": kw.get("app_name", ""),
        "Email": kw.get("email", ""),
        "Status": kw.get("status", ""),
        "Delivered": kw.get("delivered", ""),
        "Opened": kw.get("opened", ""),
        "Replied": kw.get("replied", ""),
        "Bounced": kw.get("bounced", ""),
        "Unsubscribed": kw.get("unsubscribed", ""),
        "Valid Email": kw.get("valid_email", ""),
        "Invalid Email": kw.get("invalid_email", ""),
        "Notes": kw.get("notes", ""),
    }
    return row


def _row_get(rec: dict, *keys, default=""):
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return default


def _matches_filters(rec: dict, filters: dict) -> bool:
    """UPDATE #4 — apply all advanced filters to a single analytics row."""
    if not filters:
        return True

    # Date range
    date_from = filters.get("date_from")
    date_to   = filters.get("date_to")
    rec_date  = str(_row_get(rec, "Date"))
    if date_from and rec_date and rec_date < date_from:
        return False
    if date_to and rec_date and rec_date > date_to:
        return False

    # Simple equality / "contains" filters
    eq_map = {
        "campaign":   "Campaign",
        "lead_source":"Lead Source",
        "industry":   "Industry",
        "country":    "Country",
        "status":     "Status",
    }
    for fkey, col in eq_map.items():
        val = filters.get(fkey)
        if val:
            rec_val = str(_row_get(rec, col)).lower()
            if val.lower() not in rec_val:
                return False

    # Boolean / yes-no style filters
    bool_map = {
        "delivered":     "Delivered",
        "opened":        "Opened",
        "replied":       "Replied",
        "bounced":       "Bounced",
        "unsubscribed":  "Unsubscribed",
        "valid_email":   "Valid Email",
        "invalid_email": "Invalid Email",
    }
    for fkey, col in bool_map.items():
        val = filters.get(fkey)
        if val is not None and val != "":
            wanted = str(val).strip().lower() in ("1", "true", "yes")
            actual = str(_row_get(rec, col)).strip().lower() in ("1", "true", "yes")
            if wanted != actual:
                return False

    return True


def aggregate_analytics(records: list, filters: dict = None) -> dict:
    """
    Compute every UPDATE #3 metric from raw 'Analytics' sheet rows,
    after applying UPDATE #4 filters.
    """
    filters = filters or {}
    rows = [r for r in records if _matches_filters(r, filters)]

    def count(event):
        return sum(1 for r in rows if _row_get(r, "Event") == event)

    emails_sent      = count(EV_EMAIL_SENT)
    emails_opened    = count(EV_EMAIL_OPENED)
    replies_received = count(EV_EMAIL_REPLIED)
    bounces          = count(EV_EMAIL_BOUNCED)
    delivered        = count(EV_EMAIL_DELIVERED) or max(0, emails_sent - bounces)
    unsubscribes     = count(EV_UNSUBSCRIBED)
    clicks           = count(EV_EMAIL_CLICKED)
    pending          = count(EV_PENDING_EMAIL)
    failed           = count(EV_EMAIL_FAILED)
    skipped          = count(EV_SKIPPED_LEAD)
    invalid_emails   = count(EV_INVALID_EMAIL)
    valid_emails     = count(EV_VALID_EMAIL)
    leads_generated  = count(EV_LEAD_GENERATED)
    qualified_leads  = count(EV_QUALIFIED)
    campaigns_started   = count(EV_CAMPAIGN_START)
    campaigns_completed = count(EV_CAMPAIGN_COMPLETE)

    def pct(n, d):
        return round((n / d) * 100, 2) if d else 0.0

    metrics = {
        "emails_sent":        emails_sent,
        "emails_opened":      emails_opened,
        "open_rate":          pct(emails_opened, emails_sent),
        "replies_received":   replies_received,
        "reply_rate":         pct(replies_received, emails_sent),
        "bounces":            bounces,
        "bounce_rate":        pct(bounces, emails_sent),
        "delivered_emails":   delivered,
        "delivery_rate":      pct(delivered, emails_sent),
        "unsubscribes":       unsubscribes,
        "click_rate":         pct(clicks, emails_sent),
        "pending_emails":     pending,
        "failed_emails":      failed,
        "skipped_leads":      skipped,
        "invalid_emails":     invalid_emails,
        "valid_emails":       valid_emails,
        "leads_generated":    leads_generated,
        "qualified_leads":    qualified_leads,
        "total_campaigns":    campaigns_started,
        "active_campaigns":   max(0, campaigns_started - campaigns_completed),
        "completed_campaigns": campaigns_completed,
        "row_count":          len(rows),
    }

    # ── Daily / weekly / monthly breakdowns ────────────────────────────────
    daily, weekly, monthly = {}, {}, {}
    for r in rows:
        d = str(_row_get(r, "Date"))
        if not d:
            continue
        ev = _row_get(r, "Event")
        daily.setdefault(d, {}).setdefault(ev, 0)
        daily[d][ev] += 1

        try:
            wk = time.strftime("%Y-W%W", time.strptime(d, "%Y-%m-%d"))
            mo = d[:7]  # YYYY-MM
        except Exception:
            wk, mo = "unknown", "unknown"
        weekly.setdefault(wk, {}).setdefault(ev, 0)
        weekly[wk][ev] += 1
        monthly.setdefault(mo, {}).setdefault(ev, 0)
        monthly[mo][ev] += 1

    metrics["daily"]   = daily
    metrics["weekly"]  = weekly
    metrics["monthly"] = monthly

    return metrics
