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

# ── Global duplicate tracker ──────────────────────────────────────────────────
global_seen_ids: set = set()
global_seen_emails: set = set()

# ── Sheet-based memory ────────────────────────────────────────────────────────
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

# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 ── Multi-region app detail fetch for reliable rating detection
# ══════════════════════════════════════════════════════════════════════════════
DETAIL_FETCH_COMBOS = [
    ("en", "us"),
    ("en", "gb"),
    ("en", "au"),
]

def fetch_app_details_reliable(app_id: str):
    """
    Fetch app details from multiple regions to ensure rating data is
    reliably detected regardless of country-specific Play Store differences.
    Prefers a result that has a score; falls back to first successful result.
    """
    first_result = None
    for lang, country in DETAIL_FETCH_COMBOS:
        try:
            details = gp_app(app_id, lang=lang, country=country)
            if first_result is None:
                first_result = details
            # Prefer result with a real score
            if details.get("score") is not None and details.get("score", 0) > 0:
                return details
        except Exception:
            continue
    return first_result  # None if all combos failed

# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 ── Email Verification (Python-based, bounce-safe)
# ══════════════════════════════════════════════════════════════════════════════
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "trashmail.com",
    "yopmail.com", "throwam.com", "sharklasers.com", "guerrillamail.info",
    "guerrillamail.biz", "guerrillamail.de", "guerrillamail.net", "guerrillamail.org",
    "spam4.me", "tempmail.com", "fakeinbox.com", "maildrop.cc", "dispostable.com",
    "mailnull.com", "spamgourmet.com", "discard.email", "getnada.com",
    "tempr.email", "33mail.com", "spamex.com", "mailexpire.com",
    "spamfree24.org", "spamtrail.com", "deadaddress.com", "spambob.com",
}

EMAIL_SYNTAX_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def verify_email_syntax(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    return bool(EMAIL_SYNTAX_RE.match(email))

def verify_email_domain_dns(domain: str, timeout: int = 5) -> bool:
    """Check if the domain resolves via DNS (has a valid DNS record)."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(domain, None)
        return True
    except (socket.gaierror, socket.timeout, OSError):
        return False

def is_valid_email(email: str) -> tuple:
    """
    Full email validation pipeline:
    1. Syntax check (regex)
    2. Disposable domain check
    3. DNS domain existence check
    Returns (is_valid: bool, reason: str)
    """
    if not email:
        return False, "empty"
    email = email.strip().lower()
    if not verify_email_syntax(email):
        return False, "invalid_syntax"
    domain = email.split("@")[-1].lower()
    if domain in DISPOSABLE_DOMAINS:
        return False, "disposable_domain"
    if not verify_email_domain_dns(domain):
        return False, "domain_not_found"
    return True, "ok"

# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 ── Smarter Keyword Generation (intent-aligned, niche-focused)
# ══════════════════════════════════════════════════════════════════════════════
KEYWORD_GENERATION_SYSTEM_PROMPT = """You are a Google Play Store keyword expert specializing in finding apps that need review management or reputation improvement services.

CONTEXT: We offer a Play Store review improvement service to app developers. We target apps that either:
- Have poor ratings (1.0-2.5 stars) and need reputation recovery (Hunter Mode)
- Are brand new with no ratings yet and need their first reviews (Normal Mode)

YOUR GOAL: Generate search keywords that find REAL apps in the SAME niche as the original keyword, where developers are likely to need and pay for review improvement services.

STRICT RULES:
- Stay in the EXACT same niche/industry as the original keyword
- Do NOT drift into tangentially related industries
  BAD: "crypto wallet" -> "cryptocurrency calculator" (different intent/product type)
  GOOD: "crypto wallet" -> "bitcoin wallet mobile", "ethereum wallet app", "crypto portfolio tracker"
- Keywords must be specific enough to find real apps on Play Store (not generic)
- Focus on niches: fintech, productivity, business tools, health/fitness, education, food delivery, local services, e-commerce, utilities
- Each keyword should be a realistic 2-5 word Play Store search query
- Avoid single-word generic keywords

Return ONLY a valid JSON array of strings. No markdown, no explanation."""

def ai_gen_keywords(original: str, used: list) -> list:
    key = get_cfg("GROQ_API_KEY")
    if not key:
        push_log("GROQ_API_KEY not set")
        return []
    client = Groq(api_key=key)
    prompt = (
        f"Original keyword: '{original}'\n"
        f"Already used (do NOT repeat these): {', '.join(used) if used else 'none'}\n\n"
        f"Generate exactly 8 NEW Google Play Store search keywords in the SAME niche as '{original}'.\n"
        f"These keywords should find small/indie apps that may need review improvement services.\n"
        f"Keep intent tightly aligned — same business niche, not loosely related topics.\n"
        f"Return ONLY a JSON array of 8 strings."
    )
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": KEYWORD_GENERATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=300
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        kws = json.loads(raw)
        valid_kws = [k for k in kws if isinstance(k, str) and k.strip() and k not in used]
        push_log(f"AI keywords: {valid_kws}")
        return valid_kws
    except Exception as e:
        push_log(f"AI keyword error: {e}")
        return []

# ── AI email generation per lead ──────────────────────────────────────────────
def ai_gen_email(lead: dict, base_subject: str, base_body: str) -> tuple:
    key = get_cfg("GROQ_API_KEY")
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")

    if not key:
        return fill_template(base_subject, lead), fill_template(base_body, lead)

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
2. Only replace placeholder values with the real app details above
3. You may change at most 2-3 words in the entire body to naturally fit this app
4. Do NOT rewrite sentences, add new sentences, or remove any sentences
5. Do NOT change the greeting format, CTA, or sign-off
6. Preserve every line break and blank line from the template exactly. Use \\n for newlines in JSON.
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
    )

# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 (continued) ── passes_filter: Clean, strict mode separation
# ══════════════════════════════════════════════════════════════════════════════
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
    HUNTER MODE (hunter active=True):
      - installs <= max_installs (default 5000)
      - MUST have a valid rating (score is NOT None and score > 0)
      - score MUST be <= max_score (default 2.5)
      Targets: apps with bad ratings needing reputation recovery

    NORMAL MODE (hunter not active):
      - installs <= 10,000
      - MUST NOT have any rating (score is None or 0)
      Targets: brand-new apps with zero reviews that need first reviews

    These two modes are mutually exclusive and strictly enforced.
    """
    if hunter and hunter.get("active"):
        # ── HUNTER MODE ───────────────────────────────────────────────────────
        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score") or 2.5)
        if installs > max_inst:
            return False
        # Must have a real rating — no rating means new app, not hunter target
        if score is None or score == 0:
            return False
        if score > max_score:
            return False
        return True

    # ── NORMAL MODE ───────────────────────────────────────────────────────────
    if installs > 10_000:
        return False
    # Must NOT have any rating — targeting completely fresh apps
    if score is not None and score > 0:
        return False
    return True


def scrape_keyword(keyword: str, hunter: dict = None) -> list:
    """
    Scrape across multiple country combos; deduplicate via in-memory + sheet memory.
    Uses multi-region detail fetch for reliable rating detection (FIX 1).
    Verifies email before accepting lead (FIX 2).
    """
    global global_seen_ids, global_seen_emails
    mode_label = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🔍 Scraping [{mode_label}]: '{keyword}'")
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

            if is_duplicate_in_sheet(app_id, ""):
                global_seen_ids.add(app_id)
                push_log(f"  ⏭️  Skip (in sheet): {app_id}")
                continue

            # FIX 1: Multi-region fetch for reliable rating data
            details = fetch_app_details_reliable(app_id)
            if details is None:
                global_seen_ids.add(app_id)
                continue

            installs = details.get("minInstalls") or 0
            score    = details.get("score")
            # Normalize: treat 0.0 same as None (no real rating)
            if score is not None and score == 0.0:
                score = None

            if not passes_filter(installs, score, hunter):
                global_seen_ids.add(app_id)
                continue

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

            # FIX 2: Validate email before accepting
            valid, reason = is_valid_email(email)
            if not valid:
                global_seen_ids.add(app_id)
                push_log(f"  ❌ Email invalid ({reason}): {email}")
                continue

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
            register_in_sheet_memory(app_id, email)

            score_str = f"{score:.1f}★" if score else "new (no rating)"
            push_log(f"  ✅ [{mode_label}] {lead['app_name']} | {installs:,} installs | {score_str} | {email}")
            time.sleep(0.25)

        push_log(f"  [{country}] done. Leads so far: {len(leads)}")
        time.sleep(0.5)

    push_log(f"  📦 {len(leads)} new leads from '{keyword}'")
    sheet_log_keyword(keyword, len(leads))
    return leads

# ── Email send (with pre-send verification gate) ──────────────────────────────
def send_email(lead: dict, subject: str, body: str) -> bool:
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url or not lead.get("email"):
        push_log("EMAIL_SCRIPT_URL not set or no email")
        return False

    # FIX 2: Final verification gate before sending
    valid, reason = is_valid_email(lead["email"])
    if not valid:
        push_log(f"  ⛔ Send blocked — email invalid ({reason}): {lead['email']}")
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
            return True
        push_log(f"  ❌ Email failed: {lead['email']}: {result.get('msg','?')}")
        return False
    except Exception as e:
        push_log(f"  ❌ Email error: {e}")
        return False

# ── Master automation ─────────────────────────────────────────────────────────
def run_automation(initial_kw: str, target: int, hunter: dict = None):
    global global_seen_ids, global_seen_emails

    upd(running=True, phase="loading_sheet", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode}")

    push_log("📋 Step 0: Loading existing sheet records into memory …")
    load_sheet_memory()
    push_log(f"   Memory ready: {len(sheet_memory_ids)} IDs, {len(sheet_memory_emails)} emails")

    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY

    all_leads = []
    kws_used  = [initial_kw]
    kw_queue  = [initial_kw]

    upd(phase="scraping")

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
    upd(phase="emailing")

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

@application.route("/api/sheet_memory_status", methods=["GET"])
def api_sheet_memory_status():
    with sheet_memory_lock:
        return jsonify({
            "loaded": sheet_memory_loaded,
            "ids_count": len(sheet_memory_ids),
            "emails_count": len(sheet_memory_emails),
        })

@application.route("/api/verify_email", methods=["POST"])
def api_verify_email():
    """Test endpoint: verify a single email address."""
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "email required"}), 400
    valid, reason = is_valid_email(email)
    return jsonify({"email": email, "valid": valid, "reason": reason})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
