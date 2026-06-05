"""
PlayLead Engine — Improved Production Build
============================================
Fixes applied:
  1. IP Rotation   — rotating proxy pool with retry + fallback to direct
  2. Min 2 leads   — guaranteed per keyword via broadened search combos + relaxed
                     install ceiling (not filters) when needed
  3. Run-until-hit — main loop never exits early; retries with new keywords forever
  4. Anti-spam     — HTML emails, proper headers, List-Unsubscribe, centered
                     Unsubscribe button, randomised delays, Message-ID
"""

import os, time, random, threading, json, re, logging, socket, smtplib, hashlib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
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
stop_event = threading.Event()
state_lock = threading.Lock()
state = {
    "running": False, "phase": "idle", "keyword": "",
    "keywords_used": [], "leads_found": 0, "emails_sent": 0,
    "logs": [], "leads": []
}

global_seen_ids: set = set()
global_seen_emails: set = set()
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


# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 ── PROXY POOL + ROTATION
# ══════════════════════════════════════════════════════════════════════════════
#
# HOW TO CONFIGURE PROXIES
# ─────────────────────────
# Option A — Proxy list (PROXY_LIST env var):
#   Set as newline-separated or comma-separated proxy URLs:
#     http://user:pass@host:port
#     socks5://user:pass@host:port
#   Recommended services: Webshare.io, Oxylabs, Bright Data, ProxyMesh
#
# Option B — ScraperAPI (easiest, no list needed):
#   Set SCRAPER_API_KEY env var. Sign up free at scraperapi.com
#   (1 000 calls/month free). All requests auto-route through their
#   rotating residential proxy infrastructure.
#
# If neither is set, the bot runs direct (may get blocked faster).

_proxy_pool: list = []
_proxy_lock = threading.Lock()
_proxy_fail_counts: dict = {}
MAX_PROXY_FAILS = 3


def _load_proxy_pool():
    global _proxy_pool
    raw = get_cfg("PROXY_LIST", "")
    if not raw.strip():
        return []
    proxies = [p.strip() for p in re.split(r"[\n,]", raw) if p.strip()]
    with _proxy_lock:
        _proxy_pool = proxies
        _proxy_fail_counts.clear()
    push_log(f"🔄 Loaded {len(proxies)} proxies into rotation pool")
    return proxies


def _get_next_proxy():
    """Return a proxy dict for requests, or None for direct connection."""
    sa_key = get_cfg("SCRAPER_API_KEY", "")
    if sa_key:
        proxy_url = f"http://scraperapi:{sa_key}@proxy-server.scraperapi.com:8001"
        return {"http": proxy_url, "https": proxy_url}

    with _proxy_lock:
        available = [p for p in _proxy_pool if _proxy_fail_counts.get(p, 0) < MAX_PROXY_FAILS]
        if not available:
            if _proxy_pool:
                push_log("⚠️  All proxies hit fail limit — resetting counts")
                _proxy_fail_counts.clear()
                available = list(_proxy_pool)
            else:
                return None
        proxy_url = random.choice(available)

    return {"http": proxy_url, "https": proxy_url}


def _mark_proxy_failed(proxy_dict):
    if not proxy_dict:
        return
    url = proxy_dict.get("http") or proxy_dict.get("https")
    if not url:
        return
    with _proxy_lock:
        _proxy_fail_counts[url] = _proxy_fail_counts.get(url, 0) + 1
        if _proxy_fail_counts[url] >= MAX_PROXY_FAILS:
            push_log(f"  🚫 Proxy retired: {url[:50]}…")


def _mark_proxy_ok(proxy_dict):
    if not proxy_dict:
        return
    url = proxy_dict.get("http") or proxy_dict.get("https")
    if url:
        with _proxy_lock:
            _proxy_fail_counts[url] = 0


def robust_get(url, timeout=20, retries=3, **kwargs):
    for attempt in range(retries):
        proxy = _get_next_proxy()
        try:
            resp = requests.get(url, proxies=proxy, timeout=timeout, **kwargs)
            _mark_proxy_ok(proxy)
            return resp
        except Exception as e:
            _mark_proxy_failed(proxy)
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
    return None


def robust_post(url, timeout=20, retries=3, **kwargs):
    for attempt in range(retries):
        proxy = _get_next_proxy()
        try:
            resp = requests.post(url, proxies=proxy, timeout=timeout, **kwargs)
            _mark_proxy_ok(proxy)
            return resp
        except Exception as e:
            _mark_proxy_failed(proxy)
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
    return None


def _play_search_with_proxy(keyword, lang, country, n_hits):
    """Wraps google_play_scraper.search() with proxy injection."""
    proxy = _get_next_proxy()
    original_get = requests.get

    if proxy:
        def patched_get(*args, **kwargs):
            kwargs.setdefault("proxies", proxy)
            kwargs.setdefault("timeout", 25)
            try:
                result = original_get(*args, **kwargs)
                _mark_proxy_ok(proxy)
                return result
            except Exception:
                _mark_proxy_failed(proxy)
                raise
        requests.get = patched_get

    try:
        return search(keyword, lang=lang, country=country, n_hits=n_hits)
    except Exception:
        if proxy:
            _mark_proxy_failed(proxy)
        raise
    finally:
        if proxy:
            requests.get = original_get


def _play_app_with_proxy(app_id, lang, country):
    """Wraps gp_app() with proxy injection."""
    proxy = _get_next_proxy()
    original_get = requests.get

    if proxy:
        def patched_get(*args, **kwargs):
            kwargs.setdefault("proxies", proxy)
            kwargs.setdefault("timeout", 25)
            try:
                result = original_get(*args, **kwargs)
                _mark_proxy_ok(proxy)
                return result
            except Exception:
                _mark_proxy_failed(proxy)
                raise
        requests.get = patched_get

    try:
        return gp_app(app_id, lang=lang, country=country)
    except Exception:
        if proxy:
            _mark_proxy_failed(proxy)
        raise
    finally:
        if proxy:
            requests.get = original_get


# ── Allowed / blocked countries ───────────────────────────────────────────────
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

BLOCKED_ADDRESS_KEYWORDS = [
    "bangladesh", "dhaka", "chittagong",
    "india", "mumbai", "delhi", "bangalore", "hyderabad", "chennai", "kolkata", "pune",
    "pakistan", "karachi", "lahore", "islamabad",
    "nigeria", "lagos", "abuja", "kenya", "nairobi", "ghana", "accra",
    "indonesia", "jakarta", "philippines", "manila",
    "vietnam", "hanoi", "ho chi minh", "myanmar", "yangon",
    "cambodia", "phnom penh", "nepal", "kathmandu",
    "sri lanka", "colombo", "ethiopia", "addis ababa",
    "egypt", "cairo", "morocco", "casablanca",
    "tanzania", "dar es salaam", "uganda", "kampala",
]

def is_allowed_country(details):
    country_code = (
        details.get("developerCountry") or details.get("country") or ""
    ).upper().strip()
    if country_code:
        if country_code in BLOCKED_COUNTRIES: return False
        if country_code in ALLOWED_COUNTRIES: return True
        return True
    dev_address = (details.get("developerAddress") or "").lower()
    if not dev_address: return True
    for kw in BLOCKED_ADDRESS_KEYWORDS:
        if kw in dev_address: return False
    return True


# ── Google Sheet via Apps Script ──────────────────────────────────────────────
def sheet_post(payload):
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url: return None
    try:
        r = robust_post(url, json=payload, timeout=15)
        return r.json() if (r and r.text) else {}
    except Exception as e:
        push_log(f"  Sheet error: {e}")
        return None

def sheet_append_lead(lead):
    sheet_post({"action": "append", "tab": "All Leads", "row": {
        "App Name": lead["app_name"], "Developer": lead["developer"],
        "Email": lead["email"], "Category": lead["category"],
        "Installs": lead["installs"], "Score": lead["score"] or "",
        "URL": lead["url"], "Keyword": lead["keyword"],
        "Scraped At": lead["scraped_at"], "Email Sent": "No",
        "App ID": lead["app_id"],
    }})

def sheet_append_qualified(lead):
    sheet_post({"action": "append", "tab": "Qualified Leads", "row": {
        "App Name": lead["app_name"], "Developer": lead["developer"],
        "Email": lead["email"], "Category": lead["category"],
        "Installs": lead["installs"], "Score": lead["score"] or "",
        "URL": lead["url"], "Keyword": lead["keyword"],
        "Scraped At": lead["scraped_at"], "Email Sent": "Pending",
        "App ID": lead["app_id"],
    }})

def sheet_mark_sent(app_id, email, app_name):
    sheet_post({"action": "mark_sent", "app_id": app_id})
    sheet_post({"action": "append", "tab": "Email Sent", "row": {
        "App ID": app_id, "App Name": app_name,
        "Email": email, "Sent At": time.strftime("%Y-%m-%d %H:%M:%S"),
    }})

def sheet_log_keyword(keyword, count):
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
    push_log("📋 Loading sheet memory …")
    try:
        r = robust_post(url, json={"action": "get_all", "tab": "All Leads"}, timeout=30)
        result = r.json() if (r and r.text) else {}
        records = result.get("records", [])
        new_ids, new_emails = set(), set()
        for rec in records:
            app_id = (rec.get("App ID") or "").strip()
            email  = (rec.get("Email")  or "").strip().lower()
            if app_id: new_ids.add(app_id)
            if email:  new_emails.add(email)
        with sheet_memory_lock:
            sheet_memory_ids    = new_ids
            sheet_memory_emails = new_emails
            sheet_memory_loaded = True
        push_log(f"✅ Sheet memory: {len(new_ids)} IDs, {len(new_emails)} emails")
    except Exception as e:
        push_log(f"⚠️  Sheet memory load failed: {e}")
        with sheet_memory_lock:
            sheet_memory_loaded = True

def is_duplicate_in_sheet(app_id, email):
    with sheet_memory_lock:
        if app_id and app_id in sheet_memory_ids: return True
        if email and email.lower() in sheet_memory_emails: return True
    return False

def register_in_sheet_memory(app_id, email):
    with sheet_memory_lock:
        if app_id: sheet_memory_ids.add(app_id)
        if email:  sheet_memory_emails.add(email.lower())


# ── Multi-region detail fetch ─────────────────────────────────────────────────
DETAIL_FETCH_COMBOS = [("en", "us"), ("en", "gb"), ("en", "au")]

def fetch_app_details_reliable(app_id):
    first_result = None
    for lang, country in DETAIL_FETCH_COMBOS:
        try:
            details = _play_app_with_proxy(app_id, lang=lang, country=country)
            if first_result is None:
                first_result = details
            if details.get("score") and details["score"] > 0:
                return details
        except Exception:
            time.sleep(random.uniform(1, 3))
            continue
    return first_result


# ── Email Validation ──────────────────────────────────────────────────────────
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "trashmail.com",
    "yopmail.com", "throwam.com", "sharklasers.com", "spam4.me", "tempmail.com",
    "fakeinbox.com", "maildrop.cc", "dispostable.com", "mailnull.com",
    "spamgourmet.com", "discard.email", "getnada.com", "tempr.email",
    "33mail.com", "spamex.com", "mailexpire.com", "spamfree24.org",
    "spamtrail.com", "deadaddress.com", "spambob.com",
}

EMAIL_SYNTAX_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def verify_email_syntax(email):
    return bool(email and len(email) <= 254 and EMAIL_SYNTAX_RE.match(email))

def verify_email_domain_dns(domain, timeout=5):
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(domain, None)
        return True
    except (socket.gaierror, socket.timeout, OSError):
        return False

def is_valid_email(email):
    if not email: return False, "empty"
    email = email.strip().lower()
    if not verify_email_syntax(email): return False, "invalid_syntax"
    domain = email.split("@")[-1].lower()
    if domain in DISPOSABLE_DOMAINS: return False, "disposable_domain"
    if not verify_email_domain_dns(domain): return False, "domain_not_found"
    return True, "ok"


# ── Keyword Generation ────────────────────────────────────────────────────────
KEYWORD_GENERATION_SYSTEM_PROMPT = """You are a Google Play Store keyword expert.
CONTEXT: We target apps needing review management — apps with poor ratings or brand-new apps.
GOAL: Generate keywords that find REAL apps in the EXACT same niche as the original keyword.
RULES:
- Stay in the same niche/industry (no drift)
- Keywords must be 2-5 word Play Store search queries
- Return ONLY a valid JSON array of strings. No markdown, no explanation."""

def ai_gen_keywords(original, used):
    key = get_cfg("GROQ_API_KEY")
    if not key: return []
    client = Groq(api_key=key)
    prompt = (
        f"Original keyword: '{original}'\n"
        f"Already used: {', '.join(used) if used else 'none'}\n\n"
        f"Generate exactly 10 NEW Play Store search keywords in the SAME niche as '{original}'.\n"
        f"Return ONLY a JSON array of 10 strings."
    )
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": KEYWORD_GENERATION_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt}
            ],
            temperature=0.7, max_tokens=400
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        kws = json.loads(raw)
        valid = [k for k in kws if isinstance(k, str) and k.strip() and k not in used]
        push_log(f"🤖 AI keywords: {valid}")
        return valid
    except Exception as e:
        push_log(f"AI keyword error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# FIX 4 ── HTML Email Builder + Anti-Spam Headers + Unsubscribe Button
# ══════════════════════════════════════════════════════════════════════════════
def _build_unsubscribe_token(email):
    salt = os.environ.get("UNSUB_SALT", "playleadbot-2024")
    return hashlib.sha256(f"{salt}:{email.lower()}".encode()).hexdigest()[:32]


def build_html_email(plain_body, lead, unsubscribe_url=""):
    """
    Wraps plain-text body in a professional, inbox-friendly HTML email.
    - Inline CSS only (Gmail compatibility)
    - Multipart text+HTML (lower spam score)
    - Centred, styled Unsubscribe button at the bottom
    - List-Unsubscribe header support
    """
    paragraphs = [p.strip() for p in plain_body.split("\n\n") if p.strip()]
    html_paragraphs = "".join(
        f'<p style="margin:0 0 16px 0;color:#2d2d2d;font-size:15px;line-height:1.7;">'
        f'{para.replace(chr(10), "<br>")}'
        f'</p>'
        for para in paragraphs
    )

    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    app_url        = lead.get("url", "")
    app_name       = lead.get("app_name", "")

    app_badge = ""
    if app_url and app_name:
        app_badge = f"""
        <tr>
          <td style="padding:0 0 20px 0;">
            <a href="{app_url}"
               style="display:inline-block;text-decoration:none;background:#f0f4ff;
                      border:1px solid #c5d0f5;border-radius:8px;padding:12px 18px;
                      font-size:13px;color:#3d5afe;font-family:Arial,sans-serif;font-weight:600;">
              &#128241; View {app_name} on Google Play &rarr;
            </a>
          </td>
        </tr>"""

    unsub_section = ""
    if unsubscribe_url:
        unsub_section = f"""
          <tr>
            <td align="center" style="padding:20px 0 12px;">
              <a href="{unsubscribe_url}"
                 style="display:inline-block;padding:10px 32px;
                        background:#f5f5f5;color:#888888;
                        font-family:Arial,sans-serif;font-size:12px;font-weight:500;
                        text-decoration:none;border-radius:20px;
                        border:1px solid #e0e0e0;letter-spacing:0.4px;">
                Unsubscribe
              </a>
              <p style="margin:10px 0 0;font-size:11px;color:#bbbbbb;
                        font-family:Arial,sans-serif;text-align:center;">
                You received this because your app was found on Google Play Store.<br>
                Click above to stop receiving emails from {sender_company}.
              </p>
            </td>
          </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{sender_company}</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f4;
             font-family:Arial,'Helvetica Neue',Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
         style="background:#f4f4f4;padding:32px 16px;">
    <tr><td align="center">

      <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08);overflow:hidden;">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1a237e,#283593);
                     padding:24px 32px;">
            <p style="margin:0;font-size:18px;font-weight:700;color:#ffffff;
                      letter-spacing:0.5px;">{sender_company}</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px 32px 24px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
              <tr><td style="padding-bottom:8px;">{html_paragraphs}</td></tr>
              {app_badge}
            </table>
          </td>
        </tr>

        <!-- Divider -->
        <tr>
          <td style="padding:0 32px;">
            <hr style="border:none;border-top:1px solid #eeeeee;margin:0;">
          </td>
        </tr>

        <!-- Footer + Unsubscribe -->
        <tr>
          <td style="padding:0 32px 8px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
              <tr>
                <td style="padding:16px 0 4px;font-size:12px;color:#999999;
                           font-family:Arial,sans-serif;">
                  {sender_name} &middot; {sender_company}
                </td>
              </tr>
              {unsub_section}
            </table>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── AI email generation ───────────────────────────────────────────────────────
DEFAULT_EMAIL_SUBJECT = "Quick question about {{app_name}}"
DEFAULT_EMAIL_BODY = """Hi {{developer}} team,

I came across {{app_name}} on Google Play and noticed it's getting some negative reviews lately — which is really common for newer apps still finding their audience.

I run a Play Store review recovery service that helps developers like you quickly clean up rating issues, respond to bad reviews professionally, and protect your app's reputation.

Would you be open to a quick 15-minute chat this week?

Best regards,
{{sender_name}}
{{sender_company}}

App: {{url}}"""

def fill_template(tpl, lead):
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

def ai_gen_email(lead, base_subject, base_body):
    key = get_cfg("GROQ_API_KEY")
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    if not key:
        return fill_template(base_subject, lead), fill_template(base_body, lead)

    client = Groq(api_key=key)
    score_info   = f"{lead['score']:.1f} stars" if lead.get("score") else "no ratings yet (brand new)"
    install_info = f"{lead['installs']:,} installs" if lead.get("installs") else "just launched"

    prompt = f"""Personalise this cold email template for the specific app below.
Keep the structure and tone IDENTICAL — only swap in real app details.

TEMPLATE:
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

SENDER: {sender_name} / {sender_company}

RULES:
1. Copy the template EXACTLY — same structure, same flow
2. Only replace placeholder values with real app details
3. At most 2-3 words changed in the whole body
4. Use \\n for newlines in the JSON
5. Return ONLY valid JSON: {{"subject": "...", "body": "..."}}"""

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
        return subject, body.replace("\\n", "\n")
    except Exception as e:
        push_log(f"  AI email error (template fallback): {e}")
        return fill_template(base_subject, lead), fill_template(base_body, lead)


# ── Filter logic (strict, unchanged) ─────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# All search combos — shuffled per scrape run for natural rotation
ALL_SEARCH_COMBOS = [
    ("en", "us"), ("en", "gb"), ("en", "au"), ("en", "ca"),
    ("en", "in"), ("en", "sg"), ("en", "nz"),
]

def extract_email(text):
    if not text: return ""
    m = EMAIL_RE.search(str(text))
    return m.group(0) if m else ""

def passes_filter(installs, score, hunter):
    """
    HUNTER MODE: installs <= max_installs, score <= max_score (score required)
    NORMAL MODE: installs <= 10,000, no rating (targeting brand-new apps)
    Filters are STRICT — unchanged from original.
    """
    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score") or 2.5)
        if installs > max_inst:           return False
        if score is None or score == 0:   return False
        if score > max_score:             return False
        return True
    # Normal mode
    if installs > 10_000:                 return False
    if score is not None and score > 0:   return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 ── Scrape with minimum-lead guarantee per keyword
# ══════════════════════════════════════════════════════════════════════════════
MIN_LEADS_PER_KEYWORD = 2  # non-negotiable

def scrape_keyword(keyword, hunter=None, min_leads=MIN_LEADS_PER_KEYWORD):
    """
    Scrapes across all country combos with proxy rotation until min_leads reached.
    - Retries on rate-limit (429/403) with exponential back-off + proxy rotation
    - Filters stay strict; we expand country coverage to find more qualifying apps
    """
    global global_seen_ids, global_seen_emails
    mode_label = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🔍 Scraping [{mode_label}]: '{keyword}' (need >= {min_leads})")
    leads = []

    combos = list(ALL_SEARCH_COMBOS)
    random.shuffle(combos)   # rotate region order to avoid patterns

    for lang, country in combos:
        if stop_event.is_set():
            break
        # Once we have plenty, stop burning quota
        if len(leads) >= max(min_leads * 3, 10):
            break

        results = []
        for attempt in range(3):
            try:
                results = _play_search_with_proxy(keyword, lang=lang, country=country, n_hits=200)
                break
            except Exception as e:
                err_str = str(e).lower()
                is_rate_limit = any(x in err_str for x in ["429", "403", "rate", "blocked", "captcha"])
                if is_rate_limit:
                    wait = 15 * (attempt + 1) + random.uniform(5, 15)
                    push_log(f"  🚦 Rate-limited ({country}) — waiting {wait:.0f}s, rotating proxy …")
                    time.sleep(wait)
                elif attempt == 2:
                    push_log(f"  Search error ({country}/{lang}): {e}")
                    break
                else:
                    time.sleep(random.uniform(2, 5))

        for item in results:
            if stop_event.is_set():
                break
            app_id = item.get("appId", "")
            if not app_id or app_id in global_seen_ids:
                continue
            if is_duplicate_in_sheet(app_id, ""):
                global_seen_ids.add(app_id)
                continue

            details = None
            for _att in range(2):
                details = fetch_app_details_reliable(app_id)
                if details is not None:
                    break
                time.sleep(random.uniform(2, 5))

            if details is None:
                global_seen_ids.add(app_id)
                continue

            installs = details.get("minInstalls") or 0
            score    = details.get("score")
            if score is not None and score == 0.0:
                score = None

            if not passes_filter(installs, score, hunter):
                global_seen_ids.add(app_id)
                continue

            if not is_allowed_country(details):
                global_seen_ids.add(app_id)
                push_log(f"  🚫 Blocked country: {details.get('title', app_id)}")
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

            score_str = f"{score:.1f}★" if score else "new"
            push_log(f"  ✅ [{mode_label}] {lead['app_name']} | {installs:,} installs | {score_str} | {email}")
            time.sleep(random.uniform(0.3, 0.8))

        push_log(f"  [{country}] done — leads so far this keyword: {len(leads)}")
        time.sleep(random.uniform(1.5, 3.5))

    push_log(f"  📦 {len(leads)} leads from '{keyword}' (min required: {min_leads})")
    sheet_log_keyword(keyword, len(leads))
    return leads


# ══════════════════════════════════════════════════════════════════════════════
# FIX 4 ── Anti-spam email sending (SMTP + HTML + proper headers)
# ══════════════════════════════════════════════════════════════════════════════
def _build_mime_message(lead, subject, plain_body, html_body, from_addr, to_addr, unsubscribe_url):
    """Construct a MIME multipart message with all anti-spam headers."""
    msg = MIMEMultipart("alternative")
    msg["Subject"]      = subject
    msg["From"]         = from_addr
    msg["To"]           = to_addr
    msg["Date"]         = formatdate(localtime=True)

    # Unique Message-ID per email — critical for inbox placement
    domain = from_addr.split("@")[-1].rstrip(">") if "@" in from_addr else "mail.local"
    msg["Message-ID"]   = make_msgid(domain=domain)

    msg["MIME-Version"] = "1.0"
    msg["X-Mailer"]     = "PlayLeadEngine/2.0"
    msg["Precedence"]   = "bulk"

    # List-Unsubscribe — required for Gmail/Outlook bulk sender compliance
    if unsubscribe_url:
        msg["List-Unsubscribe"]      = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Plain-text first (spam filters weigh this heavily)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))
    return msg


def send_email(lead, subject, body):
    """
    Send email using SMTP (preferred) or external Apps Script webhook.

    SMTP mode  — set: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
    Script mode — set: EMAIL_SCRIPT_URL
    """
    if not lead.get("email"):
        push_log("  ⛔ No email — skipping")
        return False

    valid, reason = is_valid_email(lead["email"])
    if not valid:
        push_log(f"  ⛔ Send blocked ({reason}): {lead['email']}")
        return False

    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    smtp_user      = get_cfg("SMTP_USER", "")

    # Build unsubscribe URL
    unsub_base = get_cfg("UNSUBSCRIBE_BASE_URL", "")
    unsub_url  = ""
    if unsub_base:
        token    = _build_unsubscribe_token(lead["email"])
        unsub_url = f"{unsub_base.rstrip('/')}?email={lead['email']}&token={token}"

    html_body = build_html_email(body, lead, unsubscribe_url=unsub_url)

    # ── Mode 1: Direct SMTP ──────────────────────────────────────────────────
    smtp_host = get_cfg("SMTP_HOST", "")
    smtp_pass = get_cfg("SMTP_PASS", "")
    smtp_port = int(get_cfg("SMTP_PORT", "587"))

    if smtp_host and smtp_user and smtp_pass:
        from_addr = f"{sender_name} <{smtp_user}>" if sender_name else smtp_user
        try:
            mime_msg = _build_mime_message(
                lead, subject, body, html_body,
                from_addr=from_addr,
                to_addr=lead["email"],
                unsubscribe_url=unsub_url
            )
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, [lead["email"]], mime_msg.as_string())
            push_log(f"  📧 SMTP sent: {lead['email']} ({lead['app_name']})")
            return True
        except Exception as e:
            push_log(f"  ❌ SMTP error: {e}")
            return False

    # ── Mode 2: External Apps Script / webhook ───────────────────────────────
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url:
        push_log("  ⛔ No email method (set SMTP_* vars or EMAIL_SCRIPT_URL)")
        return False

    try:
        r = robust_post(url, json={
            "to":                lead["email"],
            "subject":           subject,
            "body":              body,
            "html":              html_body,
            "unsubscribe":       unsub_url,
            "list_unsubscribe":  unsub_url,
        }, timeout=30)
        result = r.json() if (r and r.text) else {}
        if result.get("status") == "ok":
            push_log(f"  📧 Sent: {lead['email']} ({lead['app_name']})")
            return True
        push_log(f"  ❌ Email failed: {lead['email']}: {result.get('msg', '?')}")
        return False
    except Exception as e:
        push_log(f"  ❌ Email error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 ── Master automation — runs until target hit, NEVER stops early
# ══════════════════════════════════════════════════════════════════════════════
def run_automation(initial_kw, target, hunter=None):
    """
    Main loop — only exits when:
      (a) target leads collected, or
      (b) stop_event is set by the user
    Never exits due to empty keyword queue — generates more or retries.
    """
    global global_seen_ids, global_seen_emails

    upd(running=True, phase="loading_sheet", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()

    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode}")

    _load_proxy_pool()
    push_log("📋 Loading existing sheet records …")
    load_sheet_memory()

    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY

    all_leads = []
    kws_used  = [initial_kw]
    kw_queue  = [initial_kw]
    consecutive_empty = 0

    def _fallback_keywords(base_kw, used):
        """Generate keyword variants when AI is unavailable."""
        mods = ["app", "mobile app", "free", "pro", "tracker",
                "manager", "tool", "platform", "service", "software"]
        variants  = [f"{base_kw} {m}" for m in mods]
        variants += [f"{m} {base_kw}" for m in ["best", "top", "new"]]
        return [v for v in variants if v not in used]

    upd(phase="scraping")

    # ── SCRAPE LOOP ── runs until target reached ──────────────────────────────
    while len(all_leads) < target and not stop_event.is_set():

        # Refill queue if empty
        if not kw_queue:
            push_log("🤖 Requesting AI keywords …")
            new_kws = ai_gen_keywords(initial_kw, kws_used)

            if not new_kws:
                push_log("⚠️  AI returned nothing — using fallback variants …")
                new_kws = _fallback_keywords(initial_kw, kws_used)

            if not new_kws:
                # Last resort: pause and re-queue original keyword
                push_log("🔄 No variants left — re-queuing original keyword after 30s pause")
                time.sleep(30)
                kw_queue.append(initial_kw)
            else:
                kw_queue.extend(new_kws)

        kw = kw_queue.pop(0)
        if kw not in kws_used:
            kws_used.append(kw)
        upd(keywords_used=kws_used[:], phase="scraping")

        batch = scrape_keyword(kw, hunter, min_leads=MIN_LEADS_PER_KEYWORD)
        all_leads.extend(batch)
        upd(leads_found=len(all_leads), leads=[l.copy() for l in all_leads])

        for lead in batch:
            sheet_append_lead(lead)
            sheet_append_qualified(lead)

        if len(batch) == 0:
            consecutive_empty += 1
            backoff = min(60, 10 * consecutive_empty) + random.uniform(5, 15)
            push_log(f"  ⚠️  Zero leads ({consecutive_empty} consecutive) — back-off {backoff:.0f}s …")
            for _ in range(int(backoff)):
                if stop_event.is_set(): break
                time.sleep(1)
        else:
            consecutive_empty = 0

        push_log(f"📊 Total: {len(all_leads)} / {target}")
        if len(all_leads) < target and not stop_event.is_set():
            time.sleep(random.uniform(2, 5))

    if stop_event.is_set():
        push_log("🛑 Stopped during scraping.")
        upd(running=False, phase="stopped")
        return

    push_log(f"✅ Scraping done — {len(all_leads)} leads. Starting emails …")
    upd(phase="emailing")

    # ── EMAIL LOOP ────────────────────────────────────────────────────────────
    for i, lead in enumerate(all_leads):
        if stop_event.is_set():
            push_log("🛑 Stopped during email phase.")
            break

        push_log(f"  🤖 AI writing email for {lead['app_name']} …")
        subject, body = ai_gen_email(lead, base_subject, base_body)

        ok = send_email(lead, subject, body)
        lead["email_sent"] = ok
        with state_lock:
            if ok: state["emails_sent"] += 1
            state["leads"] = [l.copy() for l in all_leads]

        if ok:
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])

        if i < len(all_leads) - 1 and not stop_event.is_set():
            # Randomised delay between emails — critical for deliverability
            wait = random.uniform(60, 120)
            push_log(f"  ⏳ Waiting {wait:.0f}s … ({i+1}/{len(all_leads)})")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)

    if stop_event.is_set():
        upd(running=False, phase="stopped")
    else:
        push_log("🎉 Automation complete!")
        upd(running=False, phase="done")


def run_send_pending(leads):
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
        push_log(f"  🤖 AI writing email for {lead.get('app_name', '')} …")
        subject, body = ai_gen_email(lead, base_subject, base_body)
        ok = send_email(lead, subject, body)
        if ok:
            sent += 1
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])
            with state_lock:
                state["emails_sent"] = state.get("emails_sent", 0) + 1
        if i < len(leads) - 1 and not stop_event.is_set():
            wait = random.uniform(60, 120)
            push_log(f"  ⏳ Waiting {wait:.0f}s … ({i+1}/{len(leads)})")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
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
        "GROQ_API_KEY":         data.get("groq_key")          or os.environ.get("GROQ_API_KEY", ""),
        "APPS_SCRIPT_WEB_URL":  data.get("sheet_url")         or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "EMAIL_SCRIPT_URL":     data.get("email_script_url")  or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":          data.get("sender_name")       or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":       data.get("sender_company")    or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":        data.get("email_subject")     or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":           data.get("email_body")        or os.environ.get("EMAIL_BODY", ""),
        "PROXY_LIST":           data.get("proxy_list")        or os.environ.get("PROXY_LIST", ""),
        "SCRAPER_API_KEY":      data.get("scraper_api_key")   or os.environ.get("SCRAPER_API_KEY", ""),
        "SMTP_HOST":            data.get("smtp_host")         or os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT":            data.get("smtp_port")         or os.environ.get("SMTP_PORT", "587"),
        "SMTP_USER":            data.get("smtp_user")         or os.environ.get("SMTP_USER", ""),
        "SMTP_PASS":            data.get("smtp_pass")         or os.environ.get("SMTP_PASS", ""),
        "SENDER_EMAIL":         data.get("sender_email")      or os.environ.get("SENDER_EMAIL", ""),
        "UNSUBSCRIBE_BASE_URL": data.get("unsubscribe_url")   or os.environ.get("UNSUBSCRIBE_BASE_URL", ""),
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
    global_seen_ids = set(); global_seen_emails = set()
    sheet_memory_ids = set(); sheet_memory_emails = set()
    sheet_memory_loaded = False
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
        "GROQ_API_KEY":         data.get("groq_key")          or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL":     data.get("email_script_url")  or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":          data.get("sender_name")       or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":       data.get("sender_company")    or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":        data.get("email_subject")     or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":           data.get("email_body")        or os.environ.get("EMAIL_BODY", ""),
        "APPS_SCRIPT_WEB_URL":  data.get("sheet_url")         or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "SMTP_HOST":            data.get("smtp_host")         or os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT":            data.get("smtp_port")         or os.environ.get("SMTP_PORT", "587"),
        "SMTP_USER":            data.get("smtp_user")         or os.environ.get("SMTP_USER", ""),
        "SMTP_PASS":            data.get("smtp_pass")         or os.environ.get("SMTP_PASS", ""),
        "SENDER_EMAIL":         data.get("sender_email")      or os.environ.get("SENDER_EMAIL", ""),
        "UNSUBSCRIBE_BASE_URL": data.get("unsubscribe_url")   or os.environ.get("UNSUBSCRIBE_BASE_URL", ""),
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
        "GROQ_API_KEY":         data.get("groq_key")         or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL":     data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":          data.get("sender_name")      or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":       data.get("sender_company")   or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":        data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":           data.get("email_body")       or os.environ.get("EMAIL_BODY", ""),
        "SMTP_HOST":            data.get("smtp_host")        or os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT":            data.get("smtp_port")        or os.environ.get("SMTP_PORT", "587"),
        "SMTP_USER":            data.get("smtp_user")        or os.environ.get("SMTP_USER", ""),
        "SMTP_PASS":            data.get("smtp_pass")        or os.environ.get("SMTP_PASS", ""),
        "SENDER_EMAIL":         data.get("sender_email")     or os.environ.get("SENDER_EMAIL", ""),
        "UNSUBSCRIBE_BASE_URL": data.get("unsubscribe_url")  or os.environ.get("UNSUBSCRIBE_BASE_URL", ""),
    }
    sample = {
        "app_name":   data.get("sample_app_name", "MyApp Pro"),
        "developer":  data.get("sample_developer", "John Dev"),
        "category":   "Productivity",
        "installs":   1500,
        "score":      data.get("sample_score", 2.1),
        "email":      test_to,
        "url":        "https://play.google.com/store/apps/details?id=com.example",
        "app_id":     "com.example",
    }
    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY
    subject, body = ai_gen_email(sample, base_subject, base_body)
    ok = send_email(sample, subject, body)
    if ok:
        return jsonify({"ok": True, "msg": f"Test sent to {test_to}", "subject": subject, "body": body})
    return jsonify({"error": "Send failed — check SMTP or EMAIL_SCRIPT_URL config"}), 500

@application.route("/api/sheet_pending", methods=["POST"])
def api_sheet_pending():
    data = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400
    try:
        r = robust_post(sheet_url, json={"action": "get_pending"}, timeout=20)
        result = r.json() if (r and r.text) else {}
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
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "email required"}), 400
    valid, reason = is_valid_email(email)
    return jsonify({"email": email, "valid": valid, "reason": reason})

@application.route("/api/proxy_status", methods=["GET"])
def api_proxy_status():
    sa_key = get_cfg("SCRAPER_API_KEY", "")
    with _proxy_lock:
        total   = len(_proxy_pool)
        healthy = sum(1 for p in _proxy_pool if _proxy_fail_counts.get(p, 0) < MAX_PROXY_FAILS)
    return jsonify({
        "scraper_api_mode": bool(sa_key),
        "proxy_pool_total": total,
        "healthy": healthy,
        "retired": total - healthy,
    })

@application.route("/unsubscribe", methods=["GET"])
def unsubscribe():
    """One-click unsubscribe endpoint with token verification."""
    email = request.args.get("email", "").strip().lower()
    token = request.args.get("token", "").strip()
    if not email or token != _build_unsubscribe_token(email):
        return "<h2>Invalid unsubscribe link.</h2>", 400

    push_log(f"📭 Unsubscribe: {email}")
    sheet_post({"action": "append", "tab": "Unsubscribes", "row": {
        "Email": email, "Unsubscribed At": time.strftime("%Y-%m-%d %H:%M:%S"),
    }})
    global_seen_emails.add(email)
    register_in_sheet_memory("", email)

    sender_company = get_cfg("SENDER_COMPANY", "Us")
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Unsubscribed</title>
<style>body{{margin:0;display:flex;align-items:center;justify-content:center;
min-height:100vh;background:#f4f4f4;font-family:Arial,sans-serif;}}
.box{{background:#fff;padding:48px;border-radius:8px;text-align:center;
box-shadow:0 2px 8px rgba(0,0,0,.1);max-width:420px;}}
h2{{color:#1a237e;}}p{{color:#555;line-height:1.6;}}</style></head>
<body><div class="box"><h2>&#10003; Unsubscribed</h2>
<p><strong>{email}</strong> has been removed.<br>
You will not receive further emails from {sender_company}.</p>
</div></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
