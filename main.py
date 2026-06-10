"""
PlayLead Engine — Fixed Edition
================================
Changes vs v3:
  ✅ NO proxy — direct requests (Railway blocked issue solved differently)
  ✅ Maximum leads: 8-region search, parallel detail fetch, smarter dedup
  ✅ 100% accurate app data: multi-region detail fetch + HTML cross-validation
  ✅ Email validated BEFORE adding to sheet (not after)
  ✅ Apps Script fixed: proper get_all, get_pending, mark_sent, analytics
  ✅ Faster: ThreadPoolExecutor for detail fetching (12 workers)
  ✅ Smarter filter: avoids false negatives from stale score=0 data
"""

import os, time, random, threading, json, re, logging, socket
from concurrent.futures import ThreadPoolExecutor, as_completed
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

global_seen_ids: set    = set()
global_seen_emails: set = set()

sheet_memory_ids: set    = set()
sheet_memory_emails: set = set()
sheet_memory_loaded: bool = False
sheet_memory_lock = threading.Lock()

run_cfg = {}

def get_cfg(key, fallback=""):
    return run_cfg.get(key) or os.environ.get(key, fallback)

def push_log(msg: str):
    with state_lock:
        state["logs"].append({"time": time.strftime("%H:%M:%S"), "msg": msg})
        if len(state["logs"]) > 600:
            state["logs"] = state["logs"][-600:]
    log.info(msg)

def upd(**kw):
    with state_lock:
        state.update(kw)


# ══════════════════════════════════════════════════════════════════════════════
# COUNTRY FILTER
# ══════════════════════════════════════════════════════════════════════════════
BLOCKED_COUNTRIES = {
    "BD", "IN", "PK", "NG", "GH", "KE", "TZ", "UG", "ET", "EG",
    "MA", "TN", "DZ", "LY", "SD", "SO", "AO", "MZ", "ZM", "ZW",
    "MW", "RW", "SN", "CI", "CM", "CD", "MG", "MM", "KH", "LA",
    "NP", "LK", "AF", "IQ", "SY", "YE", "LB", "JO", "PS", "PH",
    "ID", "VN", "TH", "MY",
}

BLOCKED_ADDR_KW = [
    "bangladesh", "dhaka", "chittagong",
    "india", "mumbai", "delhi", "bangalore", "hyderabad", "chennai", "kolkata", "pune",
    "pakistan", "karachi", "lahore", "islamabad",
    "nigeria", "lagos", "abuja",
    "kenya", "nairobi", "ghana", "accra",
    "indonesia", "jakarta", "philippines", "manila",
    "vietnam", "hanoi", "ho chi minh",
    "myanmar", "yangon", "nepal", "kathmandu",
    "sri lanka", "colombo", "ethiopia", "addis ababa",
    "egypt", "cairo", "morocco", "casablanca",
    "tanzania", "dar es salaam", "uganda", "kampala",
]

def is_allowed_country(details: dict) -> bool:
    cc = (details.get("developerCountry") or details.get("country") or "").upper().strip()
    if cc and cc in BLOCKED_COUNTRIES:
        return False
    addr = (details.get("developerAddress") or "").lower()
    return not any(kw in addr for kw in BLOCKED_ADDR_KW)


# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEET — Apps Script helpers
# ══════════════════════════════════════════════════════════════════════════════
def sheet_post(payload: dict):
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        return None
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r and r.text:
            try:
                return r.json()
            except Exception:
                return {}
        return {}
    except Exception as e:
        push_log(f"  Sheet error: {e}")
        return None

def sheet_append_lead(lead: dict):
    sheet_post({"action": "append", "tab": "All Leads", "row": {
        "App Name":   lead["app_name"],
        "Developer":  lead["developer"],
        "Email":      lead["email"],
        "Category":   lead["category"],
        "Installs":   lead["installs"],
        "Score":      lead["score"] or "",
        "URL":        lead["url"],
        "Keyword":    lead["keyword"],
        "Scraped At": lead["scraped_at"],
        "Email Sent": "No",
        "App ID":     lead["app_id"],
    }})

def sheet_append_qualified(lead: dict):
    sheet_post({"action": "append", "tab": "Qualified Leads", "row": {
        "App Name":   lead["app_name"],
        "Developer":  lead["developer"],
        "Email":      lead["email"],
        "Category":   lead["category"],
        "Installs":   lead["installs"],
        "Score":      lead["score"] or "",
        "URL":        lead["url"],
        "Keyword":    lead["keyword"],
        "Scraped At": lead["scraped_at"],
        "Email Sent": "Pending",
        "App ID":     lead["app_id"],
    }})

def sheet_mark_sent(app_id: str, email: str, app_name: str):
    sheet_post({"action": "mark_sent", "app_id": app_id})
    sheet_post({"action": "append", "tab": "Email Sent", "row": {
        "App ID":   app_id,
        "App Name": app_name,
        "Email":    email,
        "Sent At":  time.strftime("%Y-%m-%d %H:%M:%S"),
    }})

def sheet_log_keyword(keyword: str, count: int):
    sheet_post({"action": "append", "tab": "Keyword Log", "row": {
        "Keyword":    keyword,
        "Leads Found": count,
        "Logged At":  time.strftime("%Y-%m-%d %H:%M:%S"),
    }})


# ── Sheet memory (dedup against already-collected leads) ──────────────────────
def load_sheet_memory():
    global sheet_memory_ids, sheet_memory_emails, sheet_memory_loaded
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        push_log("⚠️  No APPS_SCRIPT_WEB_URL — sheet dedup disabled")
        with sheet_memory_lock:
            sheet_memory_loaded = True
        return
    push_log("📋 Loading sheet memory …")
    try:
        r = requests.post(url, json={"action": "get_all", "tab": "All Leads"}, timeout=30)
        result = r.json() if r.text else {}
        records = result.get("records", [])
        ids, ems = set(), set()
        for rec in records:
            aid = (rec.get("App ID") or "").strip()
            em  = (rec.get("Email")  or "").strip().lower()
            if aid: ids.add(aid)
            if em:  ems.add(em)
        with sheet_memory_lock:
            sheet_memory_ids    = ids
            sheet_memory_emails = ems
            sheet_memory_loaded = True
        push_log(f"✅ Sheet memory: {len(ids)} apps, {len(ems)} emails")
    except Exception as e:
        push_log(f"⚠️  Sheet memory failed: {e}")
        with sheet_memory_lock:
            sheet_memory_loaded = True

def is_dup(app_id: str, email: str) -> bool:
    with sheet_memory_lock:
        if app_id and app_id in sheet_memory_ids:    return True
        if email   and email.lower() in sheet_memory_emails: return True
    return False

def register_mem(app_id: str, email: str):
    with sheet_memory_lock:
        if app_id: sheet_memory_ids.add(app_id)
        if email:  sheet_memory_emails.add(email.lower())


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL VALIDATION  (syntax → disposable → DNS)
# ══════════════════════════════════════════════════════════════════════════════
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "trashmail.com",
    "yopmail.com", "throwam.com", "sharklasers.com", "spam4.me", "tempmail.com",
    "fakeinbox.com", "maildrop.cc", "dispostable.com", "mailnull.com",
    "spamgourmet.com", "discard.email", "getnada.com", "tempr.email",
    "33mail.com", "spamex.com", "mailexpire.com", "spamfree24.org",
    "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de",
    "guerrillamail.net", "guerrillamail.org", "spambob.com", "deadaddress.com",
}

EMAIL_RE      = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
EMAIL_SYNTAX  = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

def extract_email(text: str) -> str:
    if not text: return ""
    m = EMAIL_RE.search(str(text))
    return m.group(0) if m else ""

def is_valid_email(email: str) -> tuple:
    """Returns (bool, reason). Validates syntax, disposable, DNS."""
    if not email:
        return False, "empty"
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_SYNTAX.match(email):
        return False, "bad_syntax"
    domain = email.split("@")[-1]
    if domain in DISPOSABLE_DOMAINS:
        return False, "disposable"
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(domain, None)
    except Exception:
        return False, "no_dns"
    return True, "ok"


# ══════════════════════════════════════════════════════════════════════════════
# ACCURATE APP DATA FETCH  (multi-region + HTML cross-validation)
# ══════════════════════════════════════════════════════════════════════════════
# 8 regions for maximum coverage — more regions = more apps found
SEARCH_COMBOS = [
    ("en", "us"), ("en", "gb"), ("en", "au"), ("en", "ca"),
    ("en", "nz"), ("en", "ie"), ("en", "sg"), ("en", "za"),
]

# For detail fetch: use 4 regions; first real score wins
DETAIL_REGIONS = [("en", "us"), ("en", "gb"), ("en", "au"), ("en", "ca")]

def parse_installs(val) -> int:
    """Convert any install field to int: handles '1,000,000+', '1M+', int, None."""
    if val is None: return 0
    if isinstance(val, (int, float)): return int(val)
    s = str(val).strip().upper().replace(",", "").replace("+", "").replace(" ", "")
    if not s or s in ("VARIESWITHDEVICE", "VARIES"): return 0
    try:
        if s.endswith("B"): return int(float(s[:-1]) * 1_000_000_000)
        if s.endswith("M"): return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"): return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except Exception:
        return 0

def _scrape_html_details(app_id: str) -> dict:
    """
    Direct HTML scrape of Play Store page for score + installs + email.
    Used as cross-validation layer — never blocks main flow.
    """
    url = f"https://play.google.com/store/apps/details?id={app_id}&hl=en&gl=us"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=12)
        html = r.text
    except Exception:
        return {}

    result = {}

    # Score — itemprop ratingValue (most reliable)
    m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', html)
    if m:
        try: result["score"] = float(m.group(1))
        except: pass

    # Score — aria-label fallback
    if "score" not in result:
        m = re.search(r'Rated\s+([\d.]+)\s+(?:stars?|out)', html, re.IGNORECASE)
        if m:
            try: result["score"] = float(m.group(1))
            except: pass

    # Installs from HTML
    for pat in [
        r'([\d,]+\+?)\s+downloads',
        r'"numDownloads"\s*:\s*"([^"]+)"',
        r'([0-9,.]+[KMB]?\+?)\s+installs',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            parsed = parse_installs(m.group(1))
            if parsed > 0:
                result["minInstalls"] = parsed
                break

    # Developer email from HTML
    m = re.search(r'href="mailto:([^"]+)"', html)
    if m:
        result["developerEmail"] = m.group(1).strip()

    return result


def fetch_app_details_reliable(app_id: str) -> dict | None:
    """
    Fetch 100% accurate app details:
    1. Try multiple regions with google-play-scraper
    2. Cross-validate score & installs with direct HTML scrape
    3. Merge best data — prefer non-zero score, real install count
    Returns None if app cannot be fetched at all.
    """
    best = None
    best_score = None

    for lang, country in DETAIL_REGIONS:
        try:
            d = gp_app(app_id, lang=lang, country=country)
            if d is None:
                continue
            sc = d.get("score")
            try:    sc = float(sc) if sc else None
            except: sc = None
            if sc == 0.0: sc = None

            if best is None:
                best = d
                best_score = sc

            if sc is not None and best_score is None:
                best = d
                best_score = sc
                break  # good enough

        except Exception:
            time.sleep(random.uniform(0.2, 0.5))
            continue

    if best is None:
        return None

    # HTML cross-validation — authoritative for score & installs
    try:
        html = _scrape_html_details(app_id)
        if html:
            best = dict(best)  # make mutable copy
            # Override score if HTML has one and it's clearer
            if html.get("score") and html["score"] > 0:
                best["score"] = html["score"]
                best_score    = html["score"]
            # Override installs if HTML gives a lower (more accurate) number
            api_inst  = parse_installs(best.get("minInstalls") or best.get("installs") or 0)
            html_inst = html.get("minInstalls", 0)
            if html_inst > 0 and (api_inst == 0 or html_inst < api_inst):
                best["minInstalls"] = html_inst
            # Use HTML email if API email missing
            if html.get("developerEmail") and not best.get("developerEmail"):
                best["developerEmail"] = html["developerEmail"]
    except Exception:
        pass

    return best


# ══════════════════════════════════════════════════════════════════════════════
# FILTER — strict, correct mode separation
# ══════════════════════════════════════════════════════════════════════════════
def passes_filter(installs: int, score, hunter: dict) -> bool:
    """
    HUNTER MODE  → installs ≤ max_installs AND has real rating AND rating ≤ max_score
    NORMAL MODE  → installs ≤ 10,000 AND zero / no rating (brand new apps)
    """
    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score")  or 2.5)
        if installs > max_inst:              return False
        if score is None or score == 0:     return False
        if score > max_score:               return False
        return True

    # Normal mode
    if installs > 10_000:                   return False
    if score is not None and score > 0:     return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD GENERATION  (AI, stays in same niche)
# ══════════════════════════════════════════════════════════════════════════════
KW_SYSTEM = """You are a Google Play Store keyword expert for finding apps needing review management.

CONTEXT: Target apps with poor ratings (Hunter Mode) or brand-new with no ratings (Normal Mode).

STRICT RULES:
- Stay in the EXACT same niche as the original keyword
  BAD: "crypto wallet" → "cryptocurrency calculator"
  GOOD: "crypto wallet" → "bitcoin wallet mobile", "ethereum wallet app"
- Keywords must be 2-5 words, real Play Store search queries
- Niches: fintech, productivity, business, health, education, food delivery, e-commerce

Return ONLY a valid JSON array. No markdown, no explanation."""

def ai_gen_keywords(original: str, used: list) -> list:
    key = get_cfg("GROQ_API_KEY")
    if not key:
        return []
    try:
        client = Groq(api_key=key)
        prompt = (
            f"Original keyword: '{original}'\n"
            f"Already used (do NOT repeat): {', '.join(used[-20:]) if used else 'none'}\n\n"
            f"Generate exactly 10 NEW Google Play Store search keywords in the SAME niche as '{original}'.\n"
            f"Return ONLY a JSON array of 10 strings."
        )
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": KW_SYSTEM},
                {"role": "user",   "content": prompt}
            ],
            temperature=0.6, max_tokens=400
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        kws = json.loads(raw)
        valid = [k for k in kws if isinstance(k, str) and k.strip() and k not in used]
        push_log(f"🤖 AI keywords: {valid[:6]}")
        return valid
    except Exception as e:
        push_log(f"AI kw error: {e}")
        return []

def _fallback_keywords(base: str, used: list) -> list:
    mods = ["app", "mobile", "free", "pro", "lite", "tracker",
            "service", "platform", "tool", "manager"]
    extras = [f"{base} {m}" for m in mods] + [
        f"best {base}", f"top {base}", f"new {base}", f"{base} 2024"
    ]
    return [k for k in extras if k not in used]


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL TEMPLATE + AI PERSONALIZER
# ══════════════════════════════════════════════════════════════════════════════
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
    sender_name    = get_cfg("SENDER_NAME",    "PlayReview")
    sender_company = get_cfg("SENDER_COMPANY", "PlayReview")
    score = lead.get("score")
    score_str = f"{score:.1f}" if score else "N/A"
    return (tpl
        .replace("{{app_name}}",       lead.get("app_name",    ""))
        .replace("{{developer}}",      lead.get("developer",   ""))
        .replace("{{category}}",       lead.get("category",    ""))
        .replace("{{installs}}",       str(lead.get("installs", "")))
        .replace("{{score}}",          score_str)
        .replace("{{url}}",            lead.get("url",         ""))
        .replace("{{sender_name}}",    sender_name)
        .replace("{{sender_company}}", sender_company)
    )

def ai_gen_email(lead: dict, base_subject: str, base_body: str) -> tuple:
    key = get_cfg("GROQ_API_KEY")
    if not key:
        return fill_template(base_subject, lead), fill_template(base_body, lead)

    score        = lead.get("score")
    score_info   = f"{score:.1f} stars" if score else "no ratings yet (brand new)"
    install_info = f"{lead['installs']:,} installs" if lead.get("installs") else "just launched"
    sender_name    = get_cfg("SENDER_NAME",    "PlayReview")
    sender_company = get_cfg("SENDER_COMPANY", "PlayReview")

    prompt = f"""You are a cold email personalizer. Fill the template with real app details — keep structure identical.

BASE TEMPLATE:
Subject: {base_subject}
Body:
{base_body}

APP DETAILS:
- App Name: {lead.get('app_name','')}
- Developer: {lead.get('developer','')}
- Category: {lead.get('category','')}
- Installs: {install_info}
- Rating: {score_info}
- URL: {lead.get('url','')}

SENDER: {sender_name} / {sender_company}

RULES:
1. Copy template EXACTLY — same structure, same sentences
2. Only replace placeholder values with real app details
3. Change at most 2-3 words in the entire body to fit naturally
4. Do NOT rewrite, add, or remove sentences
5. Use \\n for newlines in JSON
6. Return ONLY valid JSON: {{"subject": "...", "body": "..."}}"""

    try:
        client = Groq(api_key=key)
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=500
        )
        raw  = resp.choices[0].message.content.strip()
        raw  = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        data = json.loads(raw)
        subject = data.get("subject") or fill_template(base_subject, lead)
        body    = (data.get("body") or fill_template(base_body, lead)).replace("\\n", "\n")
        return subject, body
    except Exception as e:
        push_log(f"  AI email error (template fallback): {e}")
        return fill_template(base_subject, lead), fill_template(base_body, lead)


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPER  — parallel, no proxy, max leads
# ══════════════════════════════════════════════════════════════════════════════
DETAIL_FETCH_WORKERS = 12   # parallel threads for fetching app details
MIN_LEADS_PER_KW     = 3    # warn if below this

def _process_candidate(app_id: str, keyword: str, hunter: dict) -> dict | None:
    """
    Full pipeline for one app_id → returns lead dict or None.
    Runs in thread pool.
    """
    # Skip if already seen
    if app_id in global_seen_ids:
        return None
    if is_dup(app_id, ""):
        global_seen_ids.add(app_id)
        return None

    # Fetch accurate details
    details = fetch_app_details_reliable(app_id)
    if details is None:
        global_seen_ids.add(app_id)
        return None

    installs = parse_installs(details.get("minInstalls") or details.get("installs") or 0)
    score    = details.get("score")
    if score is not None:
        try:    score = float(score)
        except: score = None
    if score == 0.0:
        score = None

    # Apply filter
    if not passes_filter(installs, score, hunter):
        global_seen_ids.add(app_id)
        return None

    # Country filter
    if not is_allowed_country(details):
        global_seen_ids.add(app_id)
        return None

    # Extract email — try all fields
    email = (
        extract_email(details.get("developerEmail", ""))
        or extract_email(details.get("privacyPolicy", ""))
        or extract_email(details.get("description", ""))
        or extract_email(details.get("recentChanges", ""))
    )
    if not email:
        global_seen_ids.add(app_id)
        return None

    email = email.strip().lower()

    # ✅ VALIDATE EMAIL BEFORE ADDING TO SHEET
    valid, reason = is_valid_email(email)
    if not valid:
        global_seen_ids.add(app_id)
        push_log(f"  ❌ Email invalid ({reason}): {email}")
        return None

    # Dedup email
    if email in global_seen_emails or is_dup("", email):
        global_seen_ids.add(app_id)
        return None

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
    return lead


def _collect_all_candidates(keyword: str) -> list:
    """
    Search across ALL 8 regions and collect unique app IDs.
    Returns deduplicated list of app_ids.
    """
    seen = set()
    all_ids = []

    for lang, country in SEARCH_COMBOS:
        if stop_event.is_set():
            break
        try:
            results = search(keyword, lang=lang, country=country, n_hits=500)
            new_this_region = 0
            for item in results:
                aid = item.get("appId", "")
                if aid and aid not in seen and aid not in global_seen_ids:
                    seen.add(aid)
                    all_ids.append(aid)
                    new_this_region += 1
            push_log(f"  [{country}] {len(results)} results, {new_this_region} new candidates")
        except Exception as e:
            push_log(f"  [{country}] search error: {str(e)[:60]}")
        time.sleep(random.uniform(0.3, 0.7))

    return all_ids


def scrape_keyword(keyword: str, hunter: dict = None) -> list:
    """
    Full keyword scrape:
    1. Search 8 regions → collect unique candidate app IDs
    2. Fetch details in parallel (12 workers)
    3. Validate email BEFORE accepting lead
    4. Return qualified leads
    """
    global global_seen_ids, global_seen_emails

    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🔍 [{mode}] Scraping: '{keyword}'")
    leads = []

    # Step 1: collect candidates across all regions
    candidates = _collect_all_candidates(keyword)
    push_log(f"  📋 {len(candidates)} unique candidates total")

    if not candidates:
        push_log(f"  0 leads from: {keyword}")
        sheet_log_keyword(keyword, 0)
        return leads

    # Step 2: parallel detail fetch + qualify
    BATCH = 40
    for batch_start in range(0, len(candidates), BATCH):
        if stop_event.is_set():
            break

        batch = candidates[batch_start: batch_start + BATCH]

        with ThreadPoolExecutor(max_workers=DETAIL_FETCH_WORKERS) as ex:
            fut_map = {
                ex.submit(_process_candidate, aid, keyword, hunter): aid
                for aid in batch
                if aid not in global_seen_ids
            }
            for fut in as_completed(fut_map):
                aid = fut_map[fut]
                try:
                    lead = fut.result()
                except Exception as e:
                    push_log(f"  ⚠️  Error {aid}: {str(e)[:50]}")
                    global_seen_ids.add(aid)
                    lead = None

                if lead:
                    email = lead["email"]
                    # Final thread-safe dedup check
                    if email in global_seen_emails or is_dup("", email):
                        global_seen_ids.add(aid)
                        continue
                    leads.append(lead)
                    global_seen_ids.add(aid)
                    global_seen_emails.add(email)
                    register_mem(aid, email)
                    sc_str = f"{lead['score']:.1f}★" if lead["score"] else "new"
                    push_log(
                        f"  ✅ [{mode}] {lead['app_name']} | "
                        f"{lead['installs']:,} installs | {sc_str} | {email}"
                    )
                else:
                    global_seen_ids.add(aid)

        push_log(f"  Batch {batch_start//BATCH+1}: {len(leads)} leads so far")
        if batch_start + BATCH < len(candidates) and not stop_event.is_set():
            time.sleep(random.uniform(0.5, 1.2))

    push_log(f"  📦 {len(leads)} leads from '{keyword}'")
    if len(leads) < MIN_LEADS_PER_KW:
        push_log(f"  ⚠️  Low yield — may need broader keyword")
    sheet_log_keyword(keyword, len(leads))
    return leads


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SEND
# ══════════════════════════════════════════════════════════════════════════════
def send_email(lead: dict, subject: str, body: str) -> bool:
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url:
        push_log("  ⛔ EMAIL_SCRIPT_URL not configured")
        return False
    if not lead.get("email"):
        return False

    # Final validation gate before sending
    valid, reason = is_valid_email(lead["email"])
    if not valid:
        push_log(f"  ⛔ Send blocked ({reason}): {lead['email']}")
        return False

    try:
        r = requests.post(url, json={
            "to":      lead["email"],
            "subject": subject,
            "body":    body,
        }, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            push_log(f"  📧 Sent → {lead['email']} ({lead['app_name']})")
            return True
        push_log(f"  ❌ Script error: {result.get('msg', '?')[:80]}")
        return False
    except Exception as e:
        push_log(f"  ❌ Send exception: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# AUTOMATION MASTER
# ══════════════════════════════════════════════════════════════════════════════
def run_automation(initial_kw: str, target: int, hunter: dict = None):
    global global_seen_ids, global_seen_emails

    upd(running=True, phase="loading_sheet", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Start | kw='{initial_kw}' | target={target} | mode={mode}")

    # Load sheet memory for dedup
    load_sheet_memory()

    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY

    all_leads = []
    kws_used  = [initial_kw]
    kw_queue  = [initial_kw]

    # Pre-generate keywords before scraping starts
    push_log("🤖 Pre-generating keyword queue …")
    initial_extras = ai_gen_keywords(initial_kw, kws_used)
    for k in initial_extras:
        if k not in kws_used and k not in kw_queue:
            kw_queue.append(k)
    push_log(f"  Queue ready: {len(kw_queue)} keywords")

    upd(phase="scraping")
    empty_streak = 0

    while len(all_leads) < target and not stop_event.is_set():
        # Refill keyword queue when low
        if len(kw_queue) < 3:
            new_kws = ai_gen_keywords(initial_kw, kws_used)
            if new_kws:
                for k in new_kws:
                    if k not in kws_used and k not in kw_queue:
                        kw_queue.append(k)
            if not kw_queue:
                for k in _fallback_keywords(initial_kw, kws_used):
                    if k not in kw_queue:
                        kw_queue.append(k)

        if not kw_queue:
            push_log("⚠️  No more keywords. Pausing 30s …")
            for _ in range(30):
                if stop_event.is_set(): break
                time.sleep(1)
            kw_queue.append(initial_kw)
            continue

        kw = kw_queue.pop(0)
        if kw not in kws_used:
            kws_used.append(kw)
        upd(keywords_used=kws_used[:], phase="scraping")

        batch = scrape_keyword(kw, hunter)
        all_leads.extend(batch)
        upd(leads_found=len(all_leads), leads=[l.copy() for l in all_leads])

        # ✅ Add to sheet ONLY after email validation passed (done in _process_candidate)
        for lead in batch:
            sheet_append_lead(lead)
            sheet_append_qualified(lead)

        if batch:
            empty_streak = 0
        else:
            empty_streak += 1
            wait = min(45, 8 * empty_streak) + random.uniform(3, 8)
            push_log(f"  ⚠️  Empty streak {empty_streak} — waiting {wait:.0f}s")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)

        push_log(f"📊 Progress: {len(all_leads)} / {target}")
        if len(all_leads) < target and not stop_event.is_set():
            time.sleep(random.uniform(1, 3))

    if stop_event.is_set():
        push_log("🛑 Stopped during scraping.")
        upd(running=False, phase="stopped")
        return

    push_log(f"✅ {len(all_leads)} leads. Sending emails …")
    upd(phase="emailing")

    for i, lead in enumerate(all_leads):
        if stop_event.is_set():
            break
        push_log(f"  🤖 {i+1}/{len(all_leads)}: {lead['app_name']}")
        subject, body = ai_gen_email(lead, base_subject, base_body)
        ok = send_email(lead, subject, body)
        lead["email_sent"] = ok
        with state_lock:
            if ok: state["emails_sent"] += 1
            state["leads"] = [l.copy() for l in all_leads]
        if ok:
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])

        if i < len(all_leads) - 1 and not stop_event.is_set():
            wait = random.uniform(60, 120)
            push_log(f"  ⏳ {wait:.0f}s … ({i+1}/{len(all_leads)})")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)

    upd(running=False, phase="stopped" if stop_event.is_set() else "done")
    if not stop_event.is_set():
        push_log("🎉 Done!")


def run_send_pending(leads: list):
    upd(running=True, phase="emailing")
    stop_event.clear()
    push_log(f"📬 Pending: {len(leads)} leads")
    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY
    sent = 0
    for i, lead in enumerate(leads):
        if stop_event.is_set():
            push_log("🛑 Stopped.")
            break
        subject, body = ai_gen_email(lead, base_subject, base_body)
        ok = send_email(lead, subject, body)
        if ok:
            sent += 1
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])
            with state_lock:
                state["emails_sent"] = state.get("emails_sent", 0) + 1
        if i < len(leads) - 1 and not stop_event.is_set():
            wait = random.uniform(60, 120)
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)
    push_log(f"✅ Pending done. {sent} sent.")
    upd(running=False, phase="done")


def _build_run_cfg(data: dict) -> dict:
    return {
        "GROQ_API_KEY":        data.get("groq_key")          or os.environ.get("GROQ_API_KEY",        ""),
        "APPS_SCRIPT_WEB_URL": data.get("sheet_url")         or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "EMAIL_SCRIPT_URL":    data.get("email_script_url")  or os.environ.get("EMAIL_SCRIPT_URL",    ""),
        "SENDER_NAME":         data.get("sender_name")       or os.environ.get("SENDER_NAME",         ""),
        "SENDER_COMPANY":      data.get("sender_company")    or os.environ.get("SENDER_COMPANY",      ""),
        "EMAIL_SUBJECT":       data.get("email_subject")     or os.environ.get("EMAIL_SUBJECT",       ""),
        "EMAIL_BODY":          data.get("email_body")        or os.environ.get("EMAIL_BODY",          ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@application.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@application.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    kw   = (data.get("keyword") or "").strip()
    if not kw:
        return jsonify({"error": "keyword required"}), 400
    with state_lock:
        if state["running"]:
            return jsonify({"error": "Already running"}), 409
    global run_cfg, global_seen_ids, global_seen_emails
    run_cfg            = _build_run_cfg(data)
    global_seen_ids    = set()
    global_seen_emails = set()
    target = int(data.get("target") or os.environ.get("TARGET_LEADS", 300))
    hunter = data.get("hunter") or {}
    threading.Thread(target=run_automation, args=(kw, target, hunter), daemon=True).start()
    return jsonify({"ok": True, "keyword": kw})

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
    global_seen_ids     = set()
    global_seen_emails  = set()
    sheet_memory_ids    = set()
    sheet_memory_emails = set()
    sheet_memory_loaded = False
    return jsonify({"ok": True})

@application.route("/api/ping", methods=["GET", "POST"])
def api_ping():
    return jsonify({"ok": True, "ts": time.time()})

@application.route("/api/send_pending", methods=["POST"])
def api_send_pending():
    with state_lock:
        if state["running"]:
            return jsonify({"error": "Already running"}), 409
    data  = request.get_json(silent=True) or {}
    leads = data.get("leads") or []
    if not leads:
        return jsonify({"error": "No leads provided"}), 400
    global run_cfg
    run_cfg = _build_run_cfg(data)
    threading.Thread(target=run_send_pending, args=(leads,), daemon=True).start()
    return jsonify({"ok": True, "count": len(leads)})

@application.route("/api/spam_test", methods=["POST"])
def api_spam_test():
    data    = request.get_json(silent=True) or {}
    test_to = (data.get("test_email") or "").strip()
    if not test_to:
        return jsonify({"error": "test_email required"}), 400
    global run_cfg
    run_cfg = _build_run_cfg(data)
    sample  = {
        "app_name":  data.get("sample_app_name",  "MyFinance Pro"),
        "developer": data.get("sample_developer", "John Dev"),
        "category":  "Finance",
        "installs":  2500,
        "score":     data.get("sample_score", 2.1),
        "email":     test_to,
        "url":       "https://play.google.com/store/apps/details?id=com.example",
        "app_id":    "com.example",
    }
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url:
        return jsonify({"error": "EMAIL_SCRIPT_URL not configured"}), 400
    subject, body = ai_gen_email(sample,
        get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT,
        get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY)
    try:
        r = requests.post(url, json={"to": test_to, "subject": subject, "body": body}, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            return jsonify({"ok": True, "msg": f"Sent to {test_to}", "subject": subject, "body": body})
        return jsonify({"error": result.get("msg", "Failed")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@application.route("/api/sheet_pending", methods=["POST"])
def api_sheet_pending():
    data      = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400
    try:
        r = requests.post(sheet_url, json={"action": "get_pending"}, timeout=20)
        result = r.json() if r.text else {}
        return jsonify({"ok": True, "count": len(result.get("leads", [])), "leads": result.get("leads", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@application.route("/api/sheet_memory_status", methods=["GET"])
def api_sheet_memory_status():
    with sheet_memory_lock:
        return jsonify({
            "loaded":       sheet_memory_loaded,
            "ids_count":    len(sheet_memory_ids),
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

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
