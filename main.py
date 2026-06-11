import os, time, random, threading, json, re, logging, sqlite3, uuid, hashlib
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
# These are populated from in-memory scraping deduplication
global_seen_ids: set = set()
global_seen_emails: set = set()

# ── Sheet-based memory — loaded from Google Sheet at automation start ──────────
# These persist for the entire lifetime of the server process
sheet_memory_ids: set = set()
sheet_memory_emails: set = set()
sheet_memory_loaded: bool = False
sheet_memory_lock = threading.Lock()

run_cfg = {}

# ── SQLite Email Tracking DB ───────────────────────────────────────────────────
DB_PATH = os.environ.get("SQLITE_DB_PATH", "email_tracking.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_id TEXT UNIQUE NOT NULL,
                app_id TEXT,
                app_name TEXT,
                developer TEXT,
                email TEXT NOT NULL,
                subject TEXT,
                body TEXT,
                status TEXT DEFAULT 'sent',
                sent_at TEXT,
                opened_at TEXT,
                opened_count INTEGER DEFAULT 0,
                unsubscribe_token TEXT,
                unsubscribed INTEGER DEFAULT 0,
                keyword TEXT,
                category TEXT,
                is_failed INTEGER DEFAULT 0,
                error_msg TEXT,
                delivery_detail TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"init_db: {e}")

init_db()

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

# ── Allowed developer countries (Rich/large countries only) ──────────────────
# Apps from developers in poor/small countries are skipped.
# We check the app's "developerCountry" or "country" field from Play Store.
# These are ISO-3166-1 alpha-2 country codes for ALLOWED countries.
ALLOWED_COUNTRIES = {
    "US", "GB", "CA", "AU", "NZ", "DE", "FR", "NL", "SE", "NO",
    "DK", "FI", "CH", "AT", "BE", "IE", "SG", "JP", "KR", "IL",
    "IT", "ES", "PT", "PL", "CZ", "HU", "RO", "GR", "ZA", "AE",
    "SA", "QA", "KW", "BH", "MX", "BR", "AR", "CL", "CO",
}

# Countries that are explicitly BLOCKED (poor/small countries)
BLOCKED_COUNTRIES = {
    "BD", "IN", "PK", "NG", "GH", "KE", "TZ", "UG", "ET", "EG",
    "MA", "TN", "DZ", "LY", "SD", "SO", "AO", "MZ", "ZM", "ZW",
    "MW", "RW", "SN", "CI", "CM", "CD", "MG", "MM", "KH", "LA",
    "NP", "LK", "AF", "IQ", "SY", "YE", "LB", "JO", "PS", "PH",
    "ID", "VN", "TH", "MY",
}

def is_allowed_country(details: dict) -> bool:
    """
    Check if the app developer is from an allowed country.
    google-play-scraper returns 'developerAddress' or 'country' or 'recentChangesHTML'.
    We use a best-effort approach checking available fields.
    """
    # Check explicit country code fields if available
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
        # Unknown country — allow by default (don't over-block)
        return True

    # Fallback: check developer address for country name keywords
    dev_address = (details.get("developerAddress") or "").lower()
    if not dev_address:
        # No country info at all — allow (we can't determine)
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

# ── Google Sheet via Apps Script ──────────────────────────────────────────────
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

# ── Sheet Memory: Load all existing records at startup ─────────────────────────
def load_sheet_memory():
    """
    Fetch ALL existing records from the sheet (All Leads tab) and store
    app_id and email in memory sets. This prevents duplicates across runs.

    Called once at the start of each automation run.
    The Apps Script must support {"action": "get_all"} and return:
    {"records": [{"App ID": "...", "Email": "..."}, ...]}
    """
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
    """Check if this app_id or email already exists in the sheet memory."""
    with sheet_memory_lock:
        if app_id and app_id in sheet_memory_ids:
            return True
        if email and email.lower() in sheet_memory_emails:
            return True
    return False

def register_in_sheet_memory(app_id: str, email: str):
    """Add a newly accepted lead to sheet memory so we don't add it again."""
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
def ai_gen_email(lead: dict, base_subject: str, base_body: str) -> tuple[str, str]:
    """Generate a personalized email keeping the template structure intact."""
    key = get_cfg("GROQ_API_KEY")
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")

    if not key:
        subject = fill_template(base_subject, lead)
        body    = fill_template(base_body, lead)
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
        return subject, body
    except Exception as e:
        push_log(f"  AI email error (using template fallback): {e}")
        return fill_template(base_subject, lead), fill_template(base_body, lead)

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
        .replace("{{unsubscribe_url}}","")
    )

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
    """
    Filter logic:

    HUNTER MODE (hunter active):
      - max_installs check (default 5000)
      - MUST have a rating (score must NOT be None)
      - score must be <= max_score (default 2.5)

    NORMAL MODE:
      - installs <= 10,000
      - MUST NOT have a rating (score must be None)
        → Normal mode targets brand-new apps with no reviews yet
    """
    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score") or 2.5)
        if installs > max_inst:
            return False
        # Hunter mode: app MUST have a rating (we want apps with bad ratings)
        if score is None:
            return False
        if score > max_score:
            return False
        return True

    # Normal mode: <=10,000 installs AND no rating (completely new apps)
    if installs > 10_000:
        return False
    # Normal mode: skip apps that already have a rating
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

            try:
                details = gp_app(app_id, lang="en", country="us")
            except Exception:
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
                mode_name = "Hunter" if (hunter and hunter.get("active")) else "Normal"
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
            }
            leads.append(lead)
            global_seen_ids.add(app_id)
            global_seen_emails.add(email)
            # Register in sheet memory so same run won't add again
            register_in_sheet_memory(app_id, email)

            score_str = f"{score:.1f}★" if score else "new (no rating)"
            push_log(f"  ✅ {lead['app_name']} | {installs:,} installs | {score_str} | {email}")
            time.sleep(0.25)

        push_log(f"  [{country}] done. Leads so far: {len(leads)}")
        time.sleep(0.5)

    push_log(f"  📦 {len(leads)} new leads from '{keyword}'")
    sheet_log_keyword(keyword, len(leads))
    return leads

# ── HTML email body builder ─────────────────────────────────────────────────────
def text_to_html(plain_text: str, unsubscribe_url: str = "") -> str:
    """Convert plain text to clean HTML email with unsubscribe footer."""
    import html as htmlmod
    lines = plain_text.strip().split("\n")
    paragraphs = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            paragraphs.append("")
        else:
            escaped = htmlmod.escape(stripped)
            paragraphs.append(escaped)

    body_html = "".join(f"<p style=\"margin:0 0 12px 0;font-size:14px;line-height:1.6;color:#333333;\">{p}</p>" if p else "<p style=\"margin:0 0 12px 0;\">&nbsp;</p>" for p in paragraphs)

    footer = ""
    if unsubscribe_url:
        footer = f"""
<hr style="border:none;border-top:1px solid #eeeeee;margin:24px 0 16px 0;">
<p style="font-size:12px;color:#999999;margin:0;">
  <a href="{htmlmod.escape(unsubscribe_url)}" style="color:#888888;text-decoration:underline;">Unsubscribe</a> from future emails.
</p>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f9f9f9;">
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:6px;">
<tr><td style="padding:32px 28px;font-family:Arial,Helvetica,sans-serif;">
{body_html}
{footer}
</td></tr>
<tr><td style="padding:16px 28px;background-color:#f5f5f5;border-radius:0 0 6px 6px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#aaaaaa;text-align:center;">
  This email was sent by {htmlmod.escape(get_cfg('SENDER_COMPANY', 'PlayLead'))}.<br>
  If you no longer wish to receive these emails, <a href="{htmlmod.escape(unsubscribe_url)}" style="color:#888888;">unsubscribe here</a>.
</td></tr>
</table></td></tr></table>
</body></html>"""

# ── Email send ────────────────────────────────────────────────────────────────
def send_email(lead: dict, subject: str, body: str, html_body: str = "", unsubscribe_url: str = "") -> bool:
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url or not lead.get("email"):
        push_log("EMAIL_SCRIPT_URL not set or no email")
        return False
    payload = {
        "to":             lead["email"],
        "subject":        subject,
        "body":           body,
        "from_name":      get_cfg("SENDER_NAME", "Your Name"),
        "html_body":      html_body or body,
        "unsubscribe_url": unsubscribe_url,
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            push_log(f"  📧 Sent: {lead['email']} ({lead['app_name']})")
            return True
        push_log(f"  ❌ Email failed: {lead['email']}: {result.get('msg','?')}")
        return False
    except Exception as e:
        push_log(f"  ❌ Email error: {e}")
        return False

# ── Email tracking helpers ──────────────────────────────────────────────────────
def generate_tracking_id() -> str:
    return str(uuid.uuid4())

def generate_unsubscribe_token(tracking_id: str, email: str) -> str:
    salt = get_cfg("GROQ_API_KEY", "playlead_tracking")
    return hashlib.sha256(f"{tracking_id}:{email}:{salt}".encode()).hexdigest()[:16]

def log_email_to_db(tracking_id, lead, subject, body, status="sent", error_msg=""):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO email_log 
            (tracking_id, app_id, app_name, developer, email, subject, body,
             status, sent_at, unsubscribe_token, keyword, category, is_failed, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tracking_id, lead.get("app_id", ""), lead.get("app_name", ""),
              lead.get("developer", ""), lead.get("email", ""), subject, body,
              status, time.strftime("%Y-%m-%d %H:%M:%S"),
              generate_unsubscribe_token(tracking_id, lead.get("email", "")),
              lead.get("keyword", ""), lead.get("category", ""),
              1 if status == "failed" else 0, error_msg))
        conn.commit()
    except Exception as e:
        log.warning(f"log_email_to_db: {e}")
    finally:
        conn.close()

def send_email_tracked(lead, subject, body, base_url="") -> bool:
    """Send HTML email with unsubscribe link, List-Unsubscribe header, and DB logging."""
    tracking_id = generate_tracking_id()
    token = generate_unsubscribe_token(tracking_id, lead.get("email", ""))

    if not base_url:
        base_url = get_cfg("APP_URL", "").rstrip("/")

    uns_url = f"{base_url}/api/unsubscribe?email={lead.get('email','')}&token={token}"
    body = body.replace("{{unsubscribe_url}}", uns_url)
    body_aug = body.rstrip() + f"\n\n---\nTo unsubscribe, visit: {uns_url}"

    html_body = text_to_html(body, uns_url)

    log_email_to_db(tracking_id, lead, subject, body_aug, "sent")

    ok = send_email(lead, subject, body_aug, html_body, uns_url)

    if not ok:
        conn = get_db()
        try:
            conn.execute("UPDATE email_log SET status='failed', is_failed=1 WHERE tracking_id=?", (tracking_id,))
            conn.commit()
        except Exception as e:
            log.warning(f"send_email_tracked (fail update): {e}")
        finally:
            conn.close()

    return ok

# ── Master automation ─────────────────────────────────────────────────────────
def run_automation(initial_kw: str, target: int, hunter: dict = None):
    global global_seen_ids, global_seen_emails

    upd(running=True, phase="loading_sheet", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode}")

    # ── Step 0: Load sheet memory to prevent cross-run duplicates ─────────────
    push_log("📋 Step 0: Loading existing sheet records into memory …")
    load_sheet_memory()
    push_log(f"   Memory ready: {len(sheet_memory_ids)} IDs, {len(sheet_memory_emails)} emails")

    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY

    all_leads = []
    kws_used  = [initial_kw]
    kw_queue  = [initial_kw]

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
        return

    push_log(f"✅ Scraping done. {len(all_leads)} leads. Starting emails …")

    # ── Phase 2: AI Email + Send ──────────────────────────────────────────────
    upd(phase="emailing")

    for i, lead in enumerate(all_leads):
        if stop_event.is_set():
            push_log("🛑 Stopped during email phase.")
            break

        push_log(f"  🤖 AI writing email for {lead['app_name']} …")
        subject, body = ai_gen_email(lead, base_subject, base_body)

        ok = send_email_tracked(lead, subject, body, get_cfg("APP_URL", ""))
        lead["email_sent"] = ok
        with state_lock:
            if ok:
                state["emails_sent"] += 1
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
    else:
        push_log("🎉 Automation complete!")
        upd(running=False, phase="done")

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
        ok = send_email_tracked(lead, subject, body, get_cfg("APP_URL", ""))
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
        "APP_URL":             request.host_url.rstrip("/"),
    }
    # Reset in-memory dedup for this new run (sheet memory is re-loaded fresh)
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
        "APP_URL":             request.host_url.rstrip("/"),
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
    """Fetch leads from Sheet where Email Sent = No, return count + leads list."""
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
    """Returns how many records are currently loaded in sheet memory."""
    with sheet_memory_lock:
        return jsonify({
            "loaded": sheet_memory_loaded,
            "ids_count": len(sheet_memory_ids),
            "emails_count": len(sheet_memory_emails),
        })

# ── Email Analytics Endpoints ──────────────────────────────────────────────────
@application.route("/api/track/open")
def api_track_open():
    """Tracking pixel endpoint — logs open event."""
    tid = request.args.get("tid", "")
    if not tid:
        return "", 204
    conn = get_db()
    try:
        conn.execute("""
            UPDATE email_log SET
                opened_count = opened_count + 1,
                opened_at = COALESCE(opened_at, ?),
                status = CASE WHEN status='sent' THEN 'opened' ELSE status END
            WHERE tracking_id=?
        """, (time.strftime("%Y-%m-%d %H:%M:%S"), tid))
        conn.commit()
    except Exception as e:
        log.warning(f"track/open: {e}")
    finally:
        conn.close()
    # 1x1 transparent GIF
    return b"GIF89a\x01\x00\x01\x00\x80\x01\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\n\x00\x01\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02L\x01\x00;", 200, {"Content-Type": "image/gif"}

@application.route("/api/unsubscribe")
def api_unsubscribe():
    """Handle unsubscribe link click."""
    email = request.args.get("email", "")
    token = request.args.get("token", "")
    if not email or not token:
        return "<html><body style='font-family:sans-serif;text-align:center;padding:60px 20px;background:#0a0a0f;color:#f0e6d0;'><h2>Invalid Link</h2><p>This unsubscribe link is invalid.</p></body></html>", 400
    conn = get_db()
    try:
        conn.execute("UPDATE email_log SET unsubscribed=1, status='unsubscribed' WHERE email=? AND unsubscribe_token=?", (email, token))
        conn.commit()
    except Exception as e:
        log.warning(f"unsubscribe: {e}")
    finally:
        conn.close()
    return "<html><body style='font-family:sans-serif;text-align:center;padding:60px 20px;background:#0a0a0f;color:#f0e6d0;'><h2 style='color:#4ade80;'>✅ Unsubscribed</h2><p style='color:#8b8ba8;'>You have been unsubscribed from future emails.</p></body></html>"

@application.route("/api/analytics/overview")
def api_analytics_overview():
    """Return aggregate email stats from SQLite."""
    conn = get_db()
    try:
        cur = conn.execute("""
            SELECT
                COUNT(*) as total,
                IFNULL(SUM(CASE WHEN status='sent' AND is_failed=0 THEN 1 ELSE 0 END), 0) as sent,
                IFNULL(SUM(CASE WHEN status='opened' THEN 1 ELSE 0 END), 0) as opened,
                IFNULL(SUM(CASE WHEN status='failed' OR is_failed=1 THEN 1 ELSE 0 END), 0) as failed,
                IFNULL(SUM(CASE WHEN status='bounced' THEN 1 ELSE 0 END), 0) as bounced,
                IFNULL(SUM(CASE WHEN status='spam' THEN 1 ELSE 0 END), 0) as spam,
                IFNULL(SUM(CASE WHEN unsubscribed=1 THEN 1 ELSE 0 END), 0) as unsubscribed,
                IFNULL(SUM(CASE WHEN status='sent' AND is_failed=0 AND opened_count=0 THEN 1 ELSE 0 END), 0) as pending_open
            FROM email_log
        """)
        row = dict(cur.fetchone())
        conn.close()
        return jsonify({"ok": True, "data": row})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@application.route("/api/analytics/logs")
def api_analytics_logs():
    """Return filtered, paginated email logs."""
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))
    offset = (page - 1) * limit
    status = request.args.get("status", "")
    keyword = request.args.get("keyword", "")
    category = request.args.get("category", "")
    q = request.args.get("q", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    where = []
    params = []

    if status and status != "all":
        if status == "failed":
            where.append("(is_failed=1 OR status=?)")
            params.append("failed")
        else:
            where.append("status=?")
            params.append(status)
    if keyword:
        where.append("keyword LIKE ?")
        params.append(f"%{keyword}%")
    if category:
        where.append("category=?")
        params.append(category)
    if q:
        where.append("(app_name LIKE ? OR email LIKE ? OR subject LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if date_from:
        where.append("sent_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("sent_at <= ?")
        params.append(date_to)

    where_sql = " AND ".join(where) if where else "1=1"
    fmt = request.args.get("format", "")

    conn = get_db()
    try:
        if fmt == "csv":
            cur = conn.execute(f"SELECT * FROM email_log WHERE {where_sql} ORDER BY id DESC", params)
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            import csv, io
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=["id","tracking_id","app_id","app_name","developer","email","subject","status","sent_at","opened_at","opened_count","keyword","category","unsubscribed"])
            w.writeheader()
            for r in rows:
                del r["body"]; del r["unsubscribe_token"]; del r["is_failed"]; del r["error_msg"]; del r["delivery_detail"]
                w.writerow(r)
            return out.getvalue(), 200, {"Content-Type": "text/csv", "Content-Disposition": "attachment; filename=email_log.csv"}

        cur = conn.execute(f"SELECT COUNT(*) as cnt FROM email_log WHERE {where_sql}", params)
        total = dict(cur.fetchone())["cnt"]

        cur = conn.execute(f"""
            SELECT id, tracking_id, app_name, email, subject, status,
                   sent_at, opened_at, opened_count, unsubscribed, keyword, category
            FROM email_log WHERE {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, params + [limit, offset])
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"ok": True, "data": rows, "total": total, "page": page, "limit": limit})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@application.route("/api/analytics/email/<tracking_id>")
def api_analytics_email(tracking_id):
    """Return single email detail."""
    conn = get_db()
    try:
        cur = conn.execute("SELECT * FROM email_log WHERE tracking_id=?", (tracking_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": False, "error": "Not found"}), 404
        return jsonify({"ok": True, "data": dict(row)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@application.route("/api/analytics/update_status", methods=["POST"])
def api_analytics_update_status():
    """Manually update email status (for user corrections)."""
    data = request.get_json(silent=True) or {}
    tid = data.get("tracking_id", "")
    new_status = data.get("status", "")
    if not tid or not new_status:
        return jsonify({"ok": False, "error": "tracking_id and status required"}), 400
    valid = {"sent", "opened", "failed", "bounced", "spam", "unsubscribed", "delivered"}
    if new_status not in valid:
        return jsonify({"ok": False, "error": f"Invalid status. Valid: {valid}"}), 400
    conn = get_db()
    try:
        conn.execute("UPDATE email_log SET status=? WHERE tracking_id=?", (new_status, tid))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@application.route("/api/analytics/categories")
def api_analytics_categories():
    """Return email category breakdown from SQLite."""
    conn = get_db()
    try:
        cur = conn.execute("SELECT category, COUNT(*) as cnt FROM email_log WHERE category != '' GROUP BY category ORDER BY cnt DESC")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"ok": True, "data": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
