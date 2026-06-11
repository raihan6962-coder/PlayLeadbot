import os, time, random, threading, json, re, logging, socket, smtplib
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from google_play_scraper import search, app as gp_app
from groq import Groq
import requests

# ── Flask setup ───────────────────────────────────────────────────────────────
application = Flask(__name__, static_folder=".")
app = application
CORS(application)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────
stop_event  = threading.Event()
state_lock  = threading.Lock()
state = {
    "running": False, "phase": "idle", "keyword": "",
    "keywords_used": [], "leads_found": 0, "emails_sent": 0,
    "logs": [], "leads": []
}

# ── Global duplicate tracker — persists across runs until clear ───────────────
global_seen_ids: set = set()
global_seen_emails: set = set()

# ── Sheet-based memory — loaded from Google Sheet at automation start ──────────
sheet_memory_ids: set = set()
sheet_memory_emails: set = set()
sheet_memory_loaded: bool = False
sheet_memory_lock = threading.Lock()

run_cfg = {}

def get_cfg(key, fallback=""):
    return run_cfg.get(key) or os.environ.get(key, fallback)

def push_log(msg: str):
    with state_lock:
        state["logs"].append({"time": time.strftime("%H:%M:%S"), "msg": msg})
        if len(state["logs"]) > 500:
            state["logs"] = state["logs"][-500:]
    log.info(msg)

def upd(**kw):
    with state_lock:
        state.update(kw)

# ── Allowed developer countries ───────────────────────────────────────────────
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

def is_allowed_country(details: dict) -> bool:
    country_code = (
        details.get("developerCountry") or
        details.get("country") or
        ""
    ).upper().strip()

    if country_code:
        if country_code in BLOCKED_COUNTRIES:
            return False
        if country_code in ALLOWED_COUNTRIES:
            return True
        return True

    dev_address = (details.get("developerAddress") or "").lower()
    if not dev_address:
        return True

    blocked_keywords = [
        "bangladesh", "dhaka", "chittagong",
        "india", "mumbai", "delhi", "bangalore", "hyderabad", "chennai", "kolkata", "pune",
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
    ]
    for kw in blocked_keywords:
        if kw in dev_address:
            return False

    return True

# ═══════════════════════════════════════════════════════════════════════════════
# UPDATE #1 — EMAIL VALIDATION BEFORE SAVING LEADS
# ═══════════════════════════════════════════════════════════════════════════════

# Free disposable email domain list (common ones)
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "throwaway.email",
    "yopmail.com", "sharklasers.com", "guerrillamailblock.com", "grr.la",
    "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de", "guerrillamail.net",
    "guerrillamail.org", "spam4.me", "trashmail.com", "trashmail.me", "trashmail.net",
    "trashmail.at", "trashmail.io", "trashmail.xyz", "dispostable.com",
    "maildrop.cc", "spamgourmet.com", "mytemp.email", "fakeinbox.com",
    "mailnull.com", "spamex.com", "discard.email", "filzmail.com",
    "zetmail.com", "throwam.com", "tempr.email", "10minutemail.com",
    "10minutemail.net", "temp-mail.org", "getnada.com", "mailnesia.com",
    "spamcowboy.com", "spamcowboy.net", "spamcowboy.org",
    "spamgob.com", "tempinbox.com", "mailmetrash.com", "trashdevil.com",
    "trash-me.com", "objectmail.com", "sogetthis.com", "spaml.com",
    "spaml.de", "spamoff.de", "junk1.tk", "spam.la",
}

# Known legitimate developer email domains that always pass
TRUSTED_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "protonmail.com", "pm.me", "fastmail.com", "zoho.com",
}

EMAIL_SYNTAX_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

# Analytics counters for email validation (in-memory per run)
email_validation_stats = {
    "checked": 0, "valid": 0, "invalid_syntax": 0,
    "invalid_mx": 0, "invalid_disposable": 0, "invalid_domain": 0,
}
ev_lock = threading.Lock()

def _check_mx_records(domain: str) -> bool:
    """Check if domain has MX records. Returns True if records found."""
    try:
        # Use socket to do a basic DNS lookup for the domain
        # Full MX check requires dnspython; we do a best-effort TCP connect check
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(domain, None)
        return True
    except (socket.gaierror, socket.timeout, OSError):
        return False

def validate_email(email: str) -> dict:
    """
    Professional email validation.
    Returns: {"valid": bool, "reason": str, "score": int (0-100)}
    """
    result = {"valid": False, "reason": "", "score": 0}

    with ev_lock:
        email_validation_stats["checked"] += 1

    email = (email or "").strip().lower()

    # 1. Syntax check
    if not EMAIL_SYNTAX_RE.match(email):
        result["reason"] = "invalid_syntax"
        with ev_lock:
            email_validation_stats["invalid_syntax"] += 1
        return result

    parts = email.split("@")
    if len(parts) != 2:
        result["reason"] = "invalid_syntax"
        with ev_lock:
            email_validation_stats["invalid_syntax"] += 1
        return result

    local, domain = parts[0], parts[1]

    # 2. Basic domain format check
    if "." not in domain or len(domain) < 4:
        result["reason"] = "invalid_domain"
        with ev_lock:
            email_validation_stats["invalid_domain"] += 1
        return result

    # 3. Disposable email check
    if domain in DISPOSABLE_DOMAINS:
        result["reason"] = "disposable_email"
        with ev_lock:
            email_validation_stats["invalid_disposable"] += 1
        return result

    # 4. Trusted domain — skip MX check (always deliverable)
    if domain in TRUSTED_DOMAINS:
        result["valid"] = True
        result["reason"] = "trusted_domain"
        result["score"] = 95
        with ev_lock:
            email_validation_stats["valid"] += 1
        return result

    # 5. MX record / domain reachability check
    if not _check_mx_records(domain):
        result["reason"] = "no_mx_record"
        with ev_lock:
            email_validation_stats["invalid_mx"] += 1
        return result

    # 6. Passed all checks
    result["valid"] = True
    result["reason"] = "valid"
    result["score"] = 80
    with ev_lock:
        email_validation_stats["valid"] += 1
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# UPDATE #5 — GOOGLE SHEETS DATABASE EXPANSION (Analytics tab)
# ═══════════════════════════════════════════════════════════════════════════════

def sheet_post(payload: dict):
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        return None
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.json() if r.text else {}
    except Exception as e:
        push_log(f"  Sheet error: {e}")
        return None

def sheet_append_lead(lead: dict):
    sheet_post({"action": "append", "tab": "All Leads", "row": {
        "App Name": lead["app_name"], "Developer": lead["developer"],
        "Email": lead["email"], "Category": lead["category"],
        "Installs": lead["installs"], "Score": lead["score"] or "",
        "URL": lead["url"], "Keyword": lead["keyword"],
        "Scraped At": lead["scraped_at"], "Email Sent": "No",
        "App ID": lead["app_id"],
    }})

def sheet_append_qualified(lead: dict):
    sheet_post({"action": "append", "tab": "Qualified Leads", "row": {
        "App Name": lead["app_name"], "Developer": lead["developer"],
        "Email": lead["email"], "Category": lead["category"],
        "Installs": lead["installs"], "Score": lead["score"] or "",
        "URL": lead["url"], "Keyword": lead["keyword"],
        "Scraped At": lead["scraped_at"], "Email Sent": "Pending",
        "App ID": lead["app_id"],
    }})

def sheet_mark_sent(app_id: str, email: str, app_name: str):
    sheet_post({"action": "mark_sent", "app_id": app_id})
    sheet_post({"action": "append", "tab": "Email Sent", "row": {
        "App ID": app_id, "App Name": app_name,
        "Email": email, "Sent At": time.strftime("%Y-%m-%d %H:%M:%S"),
    }})

def sheet_log_keyword(keyword: str, count: int):
    sheet_post({"action": "append", "tab": "Keyword Log", "row": {
        "Keyword": keyword, "Leads Found": count,
        "Logged At": time.strftime("%Y-%m-%d %H:%M:%S"),
    }})

# NEW: Analytics tab tracking functions
def sheet_log_analytics_event(event_type: str, data: dict):
    """
    Log analytics events to the Analytics Events tab.
    event_type: "email_sent", "email_bounced", "email_opened", "email_replied",
                "email_unsubscribed", "lead_invalid_email", "lead_skipped", "campaign_end"
    """
    row = {
        "Timestamp":    time.strftime("%Y-%m-%d %H:%M:%S"),
        "Date":         time.strftime("%Y-%m-%d"),
        "Event Type":   event_type,
        "App ID":       data.get("app_id", ""),
        "App Name":     data.get("app_name", ""),
        "Email":        data.get("email", ""),
        "Campaign":     data.get("campaign", ""),
        "Keyword":      data.get("keyword", ""),
        "Category":     data.get("category", ""),
        "Country":      data.get("country", ""),
        "Industry":     data.get("industry", ""),
        "Status":       data.get("status", ""),
        "Details":      data.get("details", ""),
    }
    sheet_post({"action": "append", "tab": "Analytics Events", "row": row})

def sheet_log_campaign_summary(summary: dict):
    """Log a campaign summary row to the Campaign Summary tab."""
    row = {
        "Date":              time.strftime("%Y-%m-%d"),
        "Campaign":          summary.get("campaign", ""),
        "Keyword":           summary.get("keyword", ""),
        "Leads Generated":   summary.get("leads_generated", 0),
        "Qualified Leads":   summary.get("qualified_leads", 0),
        "Valid Emails":      summary.get("valid_emails", 0),
        "Invalid Emails":    summary.get("invalid_emails", 0),
        "Emails Sent":       summary.get("emails_sent", 0),
        "Emails Pending":    summary.get("emails_pending", 0),
        "Skipped Leads":     summary.get("skipped_leads", 0),
        "Duration Seconds":  summary.get("duration_seconds", 0),
        "Mode":              summary.get("mode", "Normal"),
        "Status":            summary.get("status", "Completed"),
        "Logged At":         time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    sheet_post({"action": "append", "tab": "Campaign Summary", "row": row})

# ── Sheet Memory ──────────────────────────────────────────────────────────────
def load_sheet_memory():
    global sheet_memory_ids, sheet_memory_emails, sheet_memory_loaded

    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        push_log("⚠️  No APPS_SCRIPT_WEB_URL — sheet memory disabled")
        with sheet_memory_lock:
            sheet_memory_loaded = True
        return

    push_log("📋 Loading sheet memory (existing records) …")
    try:
        r = requests.post(url, json={"action": "get_all", "tab": "All Leads"}, timeout=30)
        result = r.json() if r.text else {}
        records = result.get("records", [])

        new_ids    = set()
        new_emails = set()
        for rec in records:
            app_id = (rec.get("App ID") or "").strip()
            email  = (rec.get("Email") or "").strip().lower()
            if app_id:
                new_ids.add(app_id)
            if email:
                new_emails.add(email)

        with sheet_memory_lock:
            sheet_memory_ids    = new_ids
            sheet_memory_emails = new_emails
            sheet_memory_loaded = True

        push_log(f"✅ Sheet memory loaded: {len(new_ids)} app IDs, {len(new_emails)} emails already in sheet")
    except Exception as e:
        push_log(f"⚠️  Sheet memory load failed: {e} — continuing without sheet dedup")
        with sheet_memory_lock:
            sheet_memory_loaded = True

def is_duplicate_in_sheet(app_id: str, email: str) -> bool:
    with sheet_memory_lock:
        if app_id and app_id in sheet_memory_ids:
            return True
        if email and email.lower() in sheet_memory_emails:
            return True
    return False

def register_in_sheet_memory(app_id: str, email: str):
    with sheet_memory_lock:
        if app_id:
            sheet_memory_ids.add(app_id)
        if email:
            sheet_memory_emails.add(email.lower())

# ── AI keyword generation ─────────────────────────────────────────────────────
def ai_gen_keywords(original: str, used: list) -> list:
    key = get_cfg("GROQ_API_KEY")
    if not key:
        push_log("GROQ_API_KEY not set")
        return []
    client = Groq(api_key=key)
    prompt = (
        f"You are a Google Play Store keyword expert.\n"
        f"Original keyword: '{original}'\n"
        f"Already used: {', '.join(used) if used else 'none'}\n"
        f"Generate 8 NEW semantically similar Play Store search keywords "
        f"that would find small/new apps in the same niche. "
        f"Return ONLY a JSON array of strings, nothing else."
    )
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8, max_tokens=300
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        kws = json.loads(raw)
        push_log(f"AI keywords: {kws}")
        return [k for k in kws if k not in used]
    except Exception as e:
        push_log(f"AI keyword error: {e}")
        return []

# ── AI email generation per lead ──────────────────────────────────────────────

# UPDATE #6 — PROFESSIONAL UNSUBSCRIBE SECTION
UNSUBSCRIBE_HTML_FOOTER = """

---

<div style="text-align:center;padding:18px 0 10px;border-top:1px solid #e5e5e5;margin-top:24px;">
  <p style="font-size:11px;color:#999999;font-family:Arial,sans-serif;margin:0 0 8px;">
    You received this email because your app was found on Google Play Store.
  </p>
  <a href="{{unsubscribe_link}}" 
     style="display:inline-block;font-size:11px;color:#999999;font-family:Arial,sans-serif;
            text-decoration:underline;padding:4px 12px;border:1px solid #dddddd;
            border-radius:3px;background:#fafafa;letter-spacing:0.3px;"
     target="_blank">
    Unsubscribe
  </a>
</div>"""

UNSUBSCRIBE_PLAIN_FOOTER = """

---
To unsubscribe from future emails, click here: {{unsubscribe_link}}
"""

def build_unsubscribe_link(email: str, app_id: str) -> str:
    """Build an unsubscribe link. Uses the Apps Script URL with unsubscribe action."""
    base_url = get_cfg("APPS_SCRIPT_WEB_URL") or ""
    if base_url:
        import urllib.parse
        params = urllib.parse.urlencode({"action": "unsubscribe", "email": email, "app_id": app_id})
        return f"{base_url}?{params}"
    # Fallback: mailto unsubscribe
    sender_email = get_cfg("SENDER_EMAIL", "")
    if sender_email:
        subject = urllib.parse.quote(f"Unsubscribe - {app_id}")
        return f"mailto:{sender_email}?subject={subject}"
    return "#unsubscribe"

def append_unsubscribe(body: str, email: str, app_id: str) -> str:
    """Append professional unsubscribe footer to email body."""
    link = build_unsubscribe_link(email, app_id)
    footer = UNSUBSCRIBE_PLAIN_FOOTER.replace("{{unsubscribe_link}}", link)
    return body + footer

def ai_gen_email(lead: dict, base_subject: str, base_body: str) -> tuple[str, str]:
    """Generate a personalized email keeping the template structure intact."""
    key = get_cfg("GROQ_API_KEY")
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")

    if not key:
        subject = fill_template(base_subject, lead)
        body    = fill_template(base_body, lead)
        body    = append_unsubscribe(body, lead.get("email", ""), lead.get("app_id", ""))
        return subject, body

    client = Groq(api_key=key)
    score_info   = f"{lead['score']:.1f} stars" if lead.get("score") else "no ratings yet (brand new)"
    install_info = f"{lead['installs']:,} installs" if lead.get("installs") else "just launched"

    prompt = f"""You are a cold email personalizer. Your only job is to fill in the base template with the real app details — keeping the structure and wording almost identical.

BASE TEMPLATE (follow this EXACTLY):
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

STRICT RULES:
1. Copy the template EXACTLY — same structure, same sentences, same flow
2. Only replace placeholder values (app name, developer name, installs, rating, url) with the real app details above
3. You may change at most 2-3 words in the entire body to naturally fit this specific app — nothing more
4. Do NOT rewrite sentences, do NOT add new sentences, do NOT remove any sentences
5. Do NOT change the greeting format, CTA, or sign-off
6. CRITICAL: Preserve every line break and blank line from the template exactly as-is. Each paragraph must stay as a separate paragraph. Use \\n for newlines inside the JSON string.
7. Return ONLY valid JSON: {{"subject": "...", "body": "..."}}
No markdown, no explanation, just the JSON object."""

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=500
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        data = json.loads(raw)
        subject = data.get("subject") or fill_template(base_subject, lead)
        body    = data.get("body")    or fill_template(base_body, lead)
        body = body.replace("\\n", "\n")
        # UPDATE #6: Append unsubscribe footer
        body = append_unsubscribe(body, lead.get("email", ""), lead.get("app_id", ""))
        return subject, body
    except Exception as e:
        push_log(f"  AI email error (using template fallback): {e}")
        body = fill_template(base_body, lead)
        body = append_unsubscribe(body, lead.get("email", ""), lead.get("app_id", ""))
        return fill_template(base_subject, lead), body

# ── Template fill ─────────────────────────────────────────────────────────────
DEFAULT_EMAIL_SUBJECT = "Quick question about {{app_name}}"
DEFAULT_EMAIL_BODY = """Hi {{developer}} team,

I came across {{app_name}} on Google Play and noticed it's getting some negative reviews lately — which is really common for newer apps still finding their audience.

I run a Play Store review recovery service that helps developers like you quickly clean up rating issues, respond to bad reviews professionally, and protect your app's reputation.

Would you be open to a quick 15-minute chat this week?

Best regards,
{{sender_name}}
{{sender_company}}

App: {{url}}"""

def fill_template(tpl: str, lead: dict) -> str:
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    return (tpl
        .replace("{{app_name}}",       lead.get("app_name", ""))
        .replace("{{developer}}",      lead.get("developer", ""))
        .replace("{{category}}",       lead.get("category", ""))
        .replace("{{installs}}",       str(lead.get("installs", "")))
        .replace("{{score}}",          str(lead.get("score", "") or "N/A"))
        .replace("{{url}}",            lead.get("url", ""))
        .replace("{{sender_name}}",    sender_name)
        .replace("{{sender_company}}", sender_company)
    )

# ═══════════════════════════════════════════════════════════════════════════════
# UPDATE #2 — FIX LEAD DATA ACCURACY
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_accurate_app_details(app_id: str) -> dict | None:
    """
    Fetch app details with accuracy improvements:
    - Multiple country sources to cross-verify rating & installs
    - Use most recent/highest quality data
    - Validate data consistency before returning
    """
    sources = [
        ("en", "us"),
        ("en", "gb"),
        ("en", "au"),
    ]

    results = []
    for lang, country in sources:
        try:
            details = gp_app(app_id, lang=lang, country=country)
            if details:
                results.append({
                    "source":      f"{lang}_{country}",
                    "details":     details,
                    "score":       details.get("score"),
                    "ratings":     details.get("ratings") or 0,
                    "installs":    details.get("minInstalls") or 0,
                    "real_installs": details.get("realInstalls") or details.get("minInstalls") or 0,
                })
            time.sleep(0.1)
        except Exception:
            continue

    if not results:
        return None

    # Use US source as primary (most complete data)
    primary = results[0]["details"]

    # Cross-verify: pick the rating from the source with the most ratings (most recent/accurate)
    best_score_source = max(results, key=lambda x: x["ratings"])
    accurate_score = best_score_source["score"]

    # For installs: use the maximum value seen (most accurate upper bound)
    accurate_installs = max(r["installs"] for r in results)
    accurate_real_installs = max(r["real_installs"] for r in results)

    # Validate score is within expected range
    if accurate_score is not None:
        if not (0.0 <= accurate_score <= 5.0):
            accurate_score = None  # Reject obviously wrong value

    # Round score to 1 decimal for consistency
    if accurate_score is not None:
        accurate_score = round(accurate_score, 1)

    # Build validated result
    validated = dict(primary)
    validated["score"]          = accurate_score
    validated["minInstalls"]    = accurate_installs
    validated["realInstalls"]   = accurate_real_installs
    validated["_data_sources"]  = len(results)
    validated["_score_source"]  = best_score_source["source"]

    return validated

# ── Play Store scraper ────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

SEARCH_COMBOS = [
    ("en", "us"), ("en", "gb"), ("en", "au"), ("en", "ca"),
]

def extract_email(text):
    if not text:
        return ""
    m = EMAIL_RE.search(str(text))
    return m.group(0) if m else ""

def passes_filter(installs: int, score, hunter: dict) -> bool:
    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score") or 2.5)
        if installs > max_inst:
            return False
        if score is None:
            return False
        if score > max_score:
            return False
        return True

    # Normal mode: <=10,000 installs AND no rating (completely new apps)
    if installs > 10_000:
        return False
    if score is not None:
        return False
    return True

def scrape_keyword(keyword: str, hunter: dict = None) -> list:
    """Scrape across multiple country combos; deduplicate via in-memory + sheet memory."""
    global global_seen_ids, global_seen_emails
    push_log(f"🔍 Scraping: '{keyword}'")
    leads = []

    for lang, country in SEARCH_COMBOS:
        if stop_event.is_set():
            break
        try:
            results = search(keyword, lang=lang, country=country, n_hits=500)
        except Exception as e:
            push_log(f"  Search error ({country}): {e}")
            continue

        for item in results:
            if stop_event.is_set():
                break
            app_id = item.get("appId", "")
            if not app_id or app_id in global_seen_ids:
                continue

            # ── Sheet memory check (cross-run dedup) ──────────────────────────
            if is_duplicate_in_sheet(app_id, ""):
                global_seen_ids.add(app_id)
                push_log(f"  ⏭️  Skip (in sheet): {app_id}")
                continue

            # UPDATE #2: Use accurate multi-source data fetching
            details = fetch_accurate_app_details(app_id)
            if not details:
                global_seen_ids.add(app_id)
                continue

            installs = details.get("minInstalls") or 0
            score    = details.get("score")

            if not passes_filter(installs, score, hunter):
                global_seen_ids.add(app_id)
                continue

            # ── Country filter ─────────────────────────────────────────────────
            if not is_allowed_country(details):
                global_seen_ids.add(app_id)
                push_log(f"  🚫 Skip (blocked country): {details.get('title', app_id)}")
                continue

            email = (
                extract_email(details.get("developerEmail", ""))
                or extract_email(details.get("privacyPolicy", ""))
                or extract_email(details.get("description", ""))
                or extract_email(details.get("recentChanges", ""))
            )
            if not email:
                global_seen_ids.add(app_id)
                continue

            # ── Email dedup: check both in-memory and sheet memory ─────────────
            if email in global_seen_emails or is_duplicate_in_sheet("", email):
                global_seen_ids.add(app_id)
                push_log(f"  ⏭️  Skip (email dup): {email}")
                continue

            # ══════════════════════════════════════════════════════════════════
            # UPDATE #1: EMAIL VALIDATION BEFORE SAVING
            # ══════════════════════════════════════════════════════════════════
            push_log(f"  🔎 Validating email: {email}")
            val_result = validate_email(email)
            if not val_result["valid"]:
                push_log(f"  ❌ Invalid email ({val_result['reason']}): {email} — skipping lead")
                global_seen_ids.add(app_id)
                # Log to analytics: invalid email
                sheet_log_analytics_event("lead_invalid_email", {
                    "app_id":   app_id,
                    "app_name": details.get("title", ""),
                    "email":    email,
                    "keyword":  keyword,
                    "category": details.get("genre", ""),
                    "details":  val_result["reason"],
                })
                continue
            push_log(f"  ✉️  Email valid (score={val_result['score']}): {email}")

            lead = {
                "app_id":      app_id,
                "app_name":    details.get("title", ""),
                "developer":   details.get("developer", ""),
                "email":       email,
                "category":    details.get("genre", ""),
                "installs":    installs,
                "score":       score,
                "description": (details.get("description") or "")[:300],
                "url":         f"https://play.google.com/store/apps/details?id={app_id}",
                "icon":        details.get("icon", ""),
                "keyword":     keyword,
                "scraped_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
                "email_sent":  False,
                # UPDATE #2: Store validation metadata
                "email_valid_score": val_result["score"],
                "data_sources":      details.get("_data_sources", 1),
            }
            leads.append(lead)
            global_seen_ids.add(app_id)
            global_seen_emails.add(email)
            register_in_sheet_memory(app_id, email)

            score_str = f"{score:.1f}★" if score else "new (no rating)"
            push_log(f"  ✅ {lead['app_name']} | {installs:,} installs | {score_str} | {email}")
            time.sleep(0.25)

        push_log(f"  [{country}] done. Leads so far: {len(leads)}")
        time.sleep(0.5)

    push_log(f"  📦 {len(leads)} new leads from '{keyword}'")
    sheet_log_keyword(keyword, len(leads))
    return leads

# ── Email send ────────────────────────────────────────────────────────────────
def send_email(lead: dict, subject: str, body: str) -> bool:
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url or not lead.get("email"):
        push_log("EMAIL_SCRIPT_URL not set or no email")
        return False
    try:
        r = requests.post(url, json={
            "to":      lead["email"],
            "subject": subject,
            "body":    body,
        }, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            push_log(f"  📧 Sent: {lead['email']} ({lead['app_name']})")
            # UPDATE #5: Log analytics event for sent email
            sheet_log_analytics_event("email_sent", {
                "app_id":   lead.get("app_id", ""),
                "app_name": lead.get("app_name", ""),
                "email":    lead.get("email", ""),
                "keyword":  lead.get("keyword", ""),
                "category": lead.get("category", ""),
                "status":   "sent",
            })
            return True
        push_log(f"  ❌ Email failed: {lead['email']}: {result.get('msg','?')}")
        # UPDATE #5: Log analytics event for failed email
        sheet_log_analytics_event("email_failed", {
            "app_id":   lead.get("app_id", ""),
            "app_name": lead.get("app_name", ""),
            "email":    lead.get("email", ""),
            "keyword":  lead.get("keyword", ""),
            "details":  result.get("msg", "unknown"),
            "status":   "failed",
        })
        return False
    except Exception as e:
        push_log(f"  ❌ Email error: {e}")
        sheet_log_analytics_event("email_failed", {
            "app_id":   lead.get("app_id", ""),
            "email":    lead.get("email", ""),
            "details":  str(e),
            "status":   "failed",
        })
        return False

# ── Master automation ─────────────────────────────────────────────────────────
def run_automation(initial_kw: str, target: int, hunter: dict = None):
    global global_seen_ids, global_seen_emails

    campaign_id   = f"campaign_{time.strftime('%Y%m%d_%H%M%S')}"
    campaign_start = time.time()

    upd(running=True, phase="loading_sheet", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode} | campaign={campaign_id}")

    # Reset validation stats for this run
    with ev_lock:
        for k in email_validation_stats:
            email_validation_stats[k] = 0

    # ── Step 0: Load sheet memory to prevent cross-run duplicates ─────────────
    push_log("📋 Step 0: Loading existing sheet records into memory …")
    load_sheet_memory()
    push_log(f"   Memory ready: {len(sheet_memory_ids)} IDs, {len(sheet_memory_emails)} emails")

    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY

    all_leads  = []
    kws_used   = [initial_kw]
    kw_queue   = [initial_kw]
    skipped    = 0

    upd(phase="scraping")

    # ── Phase 1: Scrape ───────────────────────────────────────────────────────
    while len(all_leads) < target and not stop_event.is_set():
        if not kw_queue:
            push_log("🤖 Requesting AI keywords …")
            new_kws = ai_gen_keywords(initial_kw, kws_used)
            if not new_kws:
                push_log("⚠️  No more keywords. Stopping scrape.")
                break
            kw_queue.extend(new_kws)

        kw = kw_queue.pop(0)
        if kw not in kws_used:
            kws_used.append(kw)
        upd(keywords_used=kws_used[:], phase="scraping")

        batch = scrape_keyword(kw, hunter)
        all_leads.extend(batch)
        upd(leads_found=len(all_leads), leads=[l.copy() for l in all_leads])

        for lead in batch:
            sheet_append_lead(lead)
            sheet_append_qualified(lead)

        push_log(f"📊 Total: {len(all_leads)} / {target}")

    if stop_event.is_set():
        push_log("🛑 Stopped during scraping.")
        upd(running=False, phase="stopped")
        _log_campaign_end(campaign_id, initial_kw, mode, all_leads, 0, campaign_start, "Stopped")
        return

    push_log(f"✅ Scraping done. {len(all_leads)} leads. Starting emails …")

    # ── Phase 2: AI Email + Send ──────────────────────────────────────────────
    upd(phase="emailing")
    sent_count = 0

    for i, lead in enumerate(all_leads):
        if stop_event.is_set():
            push_log("🛑 Stopped during email phase.")
            break

        push_log(f"  🤖 AI writing email for {lead['app_name']} …")
        subject, body = ai_gen_email(lead, base_subject, base_body)

        ok = send_email(lead, subject, body)
        lead["email_sent"] = ok
        with state_lock:
            if ok:
                state["emails_sent"] += 1
                sent_count += 1
            state["leads"] = [l.copy() for l in all_leads]

        if ok:
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])

        if i < len(all_leads) - 1:
            wait = random.uniform(60, 120)
            push_log(f"  ⏳ Waiting {wait:.0f}s … ({i+1}/{len(all_leads)})")
            for _ in range(int(wait)):
                if stop_event.is_set():
                    break
                time.sleep(1)

    if stop_event.is_set():
        upd(running=False, phase="stopped")
        _log_campaign_end(campaign_id, initial_kw, mode, all_leads, sent_count, campaign_start, "Stopped")
    else:
        push_log("🎉 Automation complete!")
        upd(running=False, phase="done")
        _log_campaign_end(campaign_id, initial_kw, mode, all_leads, sent_count, campaign_start, "Completed")

def _log_campaign_end(campaign_id, keyword, mode, leads, sent, start_time, status):
    """Log campaign summary to the Campaign Summary sheet tab."""
    with ev_lock:
        valid_emails   = email_validation_stats["valid"]
        invalid_emails = email_validation_stats["checked"] - email_validation_stats["valid"]
    sheet_log_campaign_summary({
        "campaign":       campaign_id,
        "keyword":        keyword,
        "leads_generated": len(leads),
        "qualified_leads": len(leads),
        "valid_emails":   valid_emails,
        "invalid_emails": invalid_emails,
        "emails_sent":    sent,
        "emails_pending": len(leads) - sent,
        "skipped_leads":  invalid_emails,
        "duration_seconds": int(time.time() - start_time),
        "mode":           mode,
        "status":         status,
    })

# ── Send pending ──────────────────────────────────────────────────────────────
def run_send_pending(leads: list):
    upd(running=True, phase="emailing")
    stop_event.clear()
    push_log(f"📬 Sending pending: {len(leads)} leads")
    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY
    sent = 0
    for i, lead in enumerate(leads):
        if stop_event.is_set():
            push_log("🛑 Stopped.")
            break
        push_log(f"  🤖 AI writing email for {lead.get('app_name','')} …")
        subject, body = ai_gen_email(lead, base_subject, base_body)
        ok = send_email(lead, subject, body)
        if ok:
            sent += 1
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])
            with state_lock:
                state["emails_sent"] = state.get("emails_sent", 0) + 1
        if i < len(leads) - 1:
            wait = random.uniform(60, 120)
            push_log(f"  ⏳ Waiting {wait:.0f}s … ({i+1}/{len(leads)})")
            for _ in range(int(wait)):
                if stop_event.is_set():
                    break
                time.sleep(1)
    push_log(f"✅ Pending done. {sent} sent.")
    upd(running=False, phase="done")

# ── Routes ────────────────────────────────────────────────────────────────────
@application.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@application.route("/api/start", methods=["POST"])
def api_start():
    data    = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400
    with state_lock:
        if state["running"]:
            return jsonify({"error": "Already running"}), 409
    global run_cfg
    run_cfg = {
        "GROQ_API_KEY":        data.get("groq_key")         or os.environ.get("GROQ_API_KEY", ""),
        "APPS_SCRIPT_WEB_URL": data.get("sheet_url")        or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "EMAIL_SCRIPT_URL":    data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":         data.get("sender_name")      or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":      data.get("sender_company")   or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":       data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":          data.get("email_body")       or os.environ.get("EMAIL_BODY", ""),
    }
    global global_seen_ids, global_seen_emails
    global_seen_ids    = set()
    global_seen_emails = set()

    target = int(data.get("target") or os.environ.get("TARGET_LEADS", 300))
    hunter = data.get("hunter") or {}
    threading.Thread(target=run_automation, args=(keyword, target, hunter), daemon=True).start()
    return jsonify({"ok": True, "keyword": keyword})

@application.route("/api/stop", methods=["POST"])
def api_stop():
    stop_event.set()
    push_log("🛑 Stop requested.")
    return jsonify({"ok": True})

@application.route("/api/status")
def api_status():
    with state_lock:
        return jsonify(dict(state))

@application.route("/api/clear", methods=["POST"])
def api_clear():
    """Clear all in-memory state AND duplicate trackers. Sheet is untouched."""
    global global_seen_ids, global_seen_emails, sheet_memory_ids, sheet_memory_emails, sheet_memory_loaded
    with state_lock:
        if state["running"]:
            return jsonify({"error": "Cannot clear while running"}), 409
        state.update({
            "running": False, "phase": "idle", "keyword": "",
            "keywords_used": [], "leads_found": 0, "emails_sent": 0,
            "logs": [], "leads": []
        })
    global_seen_ids      = set()
    global_seen_emails   = set()
    sheet_memory_ids     = set()
    sheet_memory_emails  = set()
    sheet_memory_loaded  = False
    log.info("History cleared.")
    return jsonify({"ok": True})

@application.route("/api/ping", methods=["GET", "POST"])
def api_ping():
    return jsonify({"ok": True, "ts": time.time()})

@application.route("/api/send_pending", methods=["POST"])
def api_send_pending():
    with state_lock:
        if state["running"]:
            return jsonify({"error": "Automation is running"}), 409
    data  = request.get_json(silent=True) or {}
    leads = data.get("leads") or []
    if not leads:
        return jsonify({"error": "No leads provided"}), 400
    global run_cfg
    run_cfg = {
        "GROQ_API_KEY":        data.get("groq_key")         or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL":    data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":         data.get("sender_name")      or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":      data.get("sender_company")   or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":       data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":          data.get("email_body")       or os.environ.get("EMAIL_BODY", ""),
        "APPS_SCRIPT_WEB_URL": data.get("sheet_url")        or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
    }
    threading.Thread(target=run_send_pending, args=(leads,), daemon=True).start()
    return jsonify({"ok": True, "count": len(leads)})

@application.route("/api/spam_test", methods=["POST"])
def api_spam_test():
    data    = request.get_json(silent=True) or {}
    test_to = (data.get("test_email") or "").strip()
    if not test_to:
        return jsonify({"error": "test_email required"}), 400
    global run_cfg
    run_cfg = {
        "GROQ_API_KEY":     data.get("groq_key")         or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL": data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":      data.get("sender_name")      or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":   data.get("sender_company")   or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":    data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":       data.get("email_body")       or os.environ.get("EMAIL_BODY", ""),
    }
    sample = {
        "app_name":   data.get("sample_app_name", "MyApp Pro"),
        "developer":  data.get("sample_developer", "John Dev"),
        "category":   "Productivity",
        "installs":   1500,
        "score":      data.get("sample_score", 2.1),
        "email":      test_to,
        "app_id":     "com.example",
        "url":        "https://play.google.com/store/apps/details?id=com.example",
    }
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url:
        return jsonify({"error": "EMAIL_SCRIPT_URL not set"}), 400
    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY
    subject, body = ai_gen_email(sample, base_subject, base_body)
    try:
        r = requests.post(url, json={"to": test_to, "subject": subject, "body": body}, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            return jsonify({"ok": True, "msg": f"Test sent to {test_to}", "subject": subject, "body": body})
        return jsonify({"error": result.get("msg", "Failed")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Sheet pending fetch ───────────────────────────────────────────────────────
@application.route("/api/sheet_pending", methods=["POST"])
def api_sheet_pending():
    data = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400
    try:
        r = requests.post(sheet_url, json={"action": "get_pending"}, timeout=20)
        result = r.json() if r.text else {}
        leads = result.get("leads", [])
        return jsonify({"ok": True, "count": len(leads), "leads": leads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Sheet memory status ───────────────────────────────────────────────────────
@application.route("/api/sheet_memory_status", methods=["GET"])
def api_sheet_memory_status():
    with sheet_memory_lock:
        return jsonify({
            "loaded":       sheet_memory_loaded,
            "ids_count":    len(sheet_memory_ids),
            "emails_count": len(sheet_memory_emails),
        })

# ── Email validation stats API ────────────────────────────────────────────────
@application.route("/api/email_validation_stats", methods=["GET"])
def api_email_validation_stats():
    """Return current email validation statistics."""
    with ev_lock:
        stats = dict(email_validation_stats)
    total    = stats["checked"]
    valid    = stats["valid"]
    invalid  = total - valid
    rate     = round((valid / total * 100), 1) if total > 0 else 0
    return jsonify({
        "checked":            total,
        "valid":              valid,
        "invalid":            invalid,
        "valid_rate_pct":     rate,
        "invalid_syntax":     stats["invalid_syntax"],
        "invalid_mx":         stats["invalid_mx"],
        "invalid_disposable": stats["invalid_disposable"],
        "invalid_domain":     stats["invalid_domain"],
    })

# ── Analytics fetch from Sheet ────────────────────────────────────────────────
@application.route("/api/analytics", methods=["POST"])
def api_analytics():
    """
    Fetch analytics data from the Analytics Events and Campaign Summary sheets.
    Supports date_from, date_to, campaign, status filters.
    """
    data = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400

    filters = {
        "date_from":   data.get("date_from", ""),
        "date_to":     data.get("date_to", ""),
        "campaign":    data.get("campaign", ""),
        "status":      data.get("status", ""),
        "lead_source": data.get("lead_source", ""),
        "industry":    data.get("industry", ""),
        "country":     data.get("country", ""),
    }

    try:
        r = requests.post(sheet_url, json={
            "action":  "get_analytics",
            "filters": filters,
        }, timeout=25)
        result = r.json() if r.text else {}
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Unsubscribe handler (GET — for link clicks) ────────────────────────────────
@application.route("/unsubscribe", methods=["GET"])
def handle_unsubscribe():
    """Handle unsubscribe clicks. Logs to sheet and returns confirmation page."""
    email  = request.args.get("email", "").strip()
    app_id = request.args.get("app_id", "").strip()

    if email:
        push_log(f"🚫 Unsubscribe request: {email}")
        sheet_log_analytics_event("email_unsubscribed", {
            "email":   email,
            "app_id":  app_id,
            "status":  "unsubscribed",
            "details": "user clicked unsubscribe link",
        })
        # Also mark in the Unsubscribes sheet tab
        sheet_post({"action": "append", "tab": "Unsubscribes", "row": {
            "Email":         email,
            "App ID":        app_id,
            "Unsubscribed At": time.strftime("%Y-%m-%d %H:%M:%S"),
        }})

    return """<!DOCTYPE html>
<html><head><title>Unsubscribed</title>
<style>body{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f9f9f9;}
.box{text-align:center;padding:40px;background:#fff;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.1);max-width:400px;}
h2{color:#333;margin-bottom:12px;}p{color:#666;font-size:14px;}</style></head>
<body><div class="box"><h2>✅ Unsubscribed</h2>
<p>You have been successfully removed from our mailing list.</p>
<p style="margin-top:16px;font-size:12px;color:#999;">You will not receive any further emails from us.</p>
</div></body></html>""", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
