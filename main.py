import os, time, random, threading, json, re, logging, socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from google_play_scraper import search, app as gp_app
from groq import Groq
import requests
import urllib.request, urllib.error

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

# ── Global duplicate tracker ──────────────────────────────────────────────────
global_seen_ids:    set = set()
global_seen_emails: set = set()

# ── Sheet-based memory ────────────────────────────────────────────────────────
sheet_memory_ids:    set  = set()
sheet_memory_emails: set  = set()
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

# ── Allowed / Blocked developer countries ─────────────────────────────────────
ALLOWED_COUNTRIES = {
    "US","GB","CA","AU","NZ","DE","FR","NL","SE","NO",
    "DK","FI","CH","AT","BE","IE","SG","JP","KR","IL",
    "IT","ES","PT","PL","CZ","HU","RO","GR","ZA","AE",
    "SA","QA","KW","BH","MX","BR","AR","CL","CO",
}
BLOCKED_COUNTRIES = {
    "BD","IN","PK","NG","GH","KE","TZ","UG","ET","EG",
    "MA","TN","DZ","LY","SD","SO","AO","MZ","ZM","ZW",
    "MW","RW","SN","CI","CM","CD","MG","MM","KH","LA",
    "NP","LK","AF","IQ","SY","YE","LB","JO","PS","PH",
    "ID","VN","TH","MY",
}

def is_allowed_country(details: dict) -> bool:
    country_code = (details.get("developerCountry") or details.get("country") or "").upper().strip()
    if country_code:
        if country_code in BLOCKED_COUNTRIES: return False
        return True
    dev_address = (details.get("developerAddress") or "").lower()
    if not dev_address: return True
    blocked_keywords = [
        "bangladesh","dhaka","chittagong",
        "india","mumbai","delhi","bangalore","hyderabad","chennai","kolkata","pune",
        "pakistan","karachi","lahore","islamabad",
        "nigeria","lagos","abuja",
        "kenya","nairobi","ghana","accra",
        "indonesia","jakarta","philippines","manila",
        "vietnam","hanoi","ho chi minh","myanmar","yangon",
        "cambodia","phnom penh","nepal","kathmandu",
        "sri lanka","colombo","ethiopia","addis ababa",
        "egypt","cairo","morocco","casablanca",
        "tanzania","dar es salaam","uganda","kampala",
    ]
    for kw in blocked_keywords:
        if kw in dev_address: return False
    return True

# ── Google Sheet via Apps Script ──────────────────────────────────────────────
def sheet_post(payload: dict):
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url: return None
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.json() if r.text else {}
    except Exception as e:
        push_log(f"  Sheet error: {e}")
        return None

def sheet_append_lead(lead: dict):
    sheet_post({"action": "append", "tab": "All Leads", "row": {
        "App Name":   lead["app_name"],  "Developer": lead["developer"],
        "Email":      lead["email"],     "Category":  lead["category"],
        "Installs":   lead["installs"],  "Score":     lead["score"] or "",
        "URL":        lead["url"],       "Keyword":   lead["keyword"],
        "Scraped At": lead["scraped_at"],"Email Sent":"No",
        "App ID":     lead["app_id"],
    }})

def sheet_append_qualified(lead: dict):
    sheet_post({"action": "append", "tab": "Qualified Leads", "row": {
        "App Name":   lead["app_name"],  "Developer": lead["developer"],
        "Email":      lead["email"],     "Category":  lead["category"],
        "Installs":   lead["installs"],  "Score":     lead["score"] or "",
        "URL":        lead["url"],       "Keyword":   lead["keyword"],
        "Scraped At": lead["scraped_at"],"Email Sent":"Pending",
        "App ID":     lead["app_id"],
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
        with sheet_memory_lock: sheet_memory_loaded = True
        return
    push_log("📋 Loading sheet memory …")
    try:
        r = requests.post(url, json={"action": "get_all", "tab": "All Leads"}, timeout=30)
        result = r.json() if r.text else {}
        records = result.get("records", [])
        new_ids, new_emails = set(), set()
        for rec in records:
            aid = (rec.get("App ID") or "").strip()
            em  = (rec.get("Email")  or "").strip().lower()
            if aid: new_ids.add(aid)
            if em:  new_emails.add(em)
        with sheet_memory_lock:
            sheet_memory_ids    = new_ids
            sheet_memory_emails = new_emails
            sheet_memory_loaded = True
        push_log(f"✅ Sheet memory: {len(new_ids)} IDs, {len(new_emails)} emails")
    except Exception as e:
        push_log(f"⚠️  Sheet memory load failed: {e}")
        with sheet_memory_lock: sheet_memory_loaded = True

def is_duplicate_in_sheet(app_id: str, email: str) -> bool:
    with sheet_memory_lock:
        if app_id and app_id in sheet_memory_ids:    return True
        if email  and email.lower() in sheet_memory_emails: return True
    return False

def register_in_sheet_memory(app_id: str, email: str):
    with sheet_memory_lock:
        if app_id: sheet_memory_ids.add(app_id)
        if email:  sheet_memory_emails.add(email.lower())

# ══════════════════════════════════════════════════════════════════════════════
# EMAIL VALIDATION
# ══════════════════════════════════════════════════════════════════════════════
DISPOSABLE_DOMAINS = {
    "mailinator.com","guerrillamail.com","10minutemail.com","trashmail.com",
    "yopmail.com","throwam.com","sharklasers.com","guerrillamail.info",
    "guerrillamail.biz","guerrillamail.de","guerrillamail.net","guerrillamail.org",
    "spam4.me","tempmail.com","fakeinbox.com","maildrop.cc","dispostable.com",
    "mailnull.com","spamgourmet.com","discard.email","getnada.com",
    "tempr.email","33mail.com","spamex.com","mailexpire.com",
    "spamfree24.org","spamtrail.com","deadaddress.com","spambob.com",
}
EMAIL_SYNTAX_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def verify_email_syntax(email: str) -> bool:
    if not email or len(email) > 254: return False
    return bool(EMAIL_SYNTAX_RE.match(email))

def verify_email_domain_dns(domain: str, timeout: int = 5) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(domain, None)
        return True
    except (socket.gaierror, socket.timeout, OSError):
        return False

def is_valid_email(email: str) -> tuple:
    if not email: return False, "empty"
    email = email.strip().lower()
    if not verify_email_syntax(email):    return False, "invalid_syntax"
    domain = email.split("@")[-1].lower()
    if domain in DISPOSABLE_DOMAINS:      return False, "disposable_domain"
    if not verify_email_domain_dns(domain): return False, "domain_not_found"
    return True, "ok"

# ══════════════════════════════════════════════════════════════════════════════
# APP DETAIL FETCH — 100% accurate, direct Play Store HTML cross-check
# ══════════════════════════════════════════════════════════════════════════════
PLAY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def _scrape_play_html(app_id: str) -> dict:
    """Direct Play Store HTML scrape for accurate score + installs."""
    url = f"https://play.google.com/store/apps/details?id={app_id}&hl=en&gl=us"
    result = {}
    try:
        req = urllib.request.Request(url, headers=PLAY_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read(500_000).decode("utf-8", errors="ignore")
    except Exception:
        return result

    # Score — try multiple patterns
    for pat in [
        r'"starRating"\s*:\s*"?([\d.]+)"?',
        r'"ratingValue"\s*:\s*"?([\d.]+)"?',
        r'itemprop="ratingValue"\s+content="([\d.]+)"',
        r'Rated\s+([\d.]+)\s+(?:stars?|out)',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 < v <= 5:
                    result["score"] = round(v, 1)
                    break
            except: pass

    # Installs — multiple patterns
    for pat in [
        r'"numDownloads"\s*:\s*"([^"]+)"',
        r'([\d,]+\+?)\s+downloads',
        r'"([\d,.]+[KMB]?\+?)"\s*,\s*"Installs"',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "").strip()
            parsed = _parse_installs(raw)
            if parsed >= 0:
                result["minInstalls"] = parsed
                break

    # Developer email from mailto
    em = re.search(r'href="mailto:([^"?&\s]+)"', html)
    if em:
        result["developerEmail"] = em.group(1).strip()

    return result

def _parse_installs(val) -> int:
    if val is None: return 0
    s = str(val).strip().replace(",", "").replace("+", "").upper()
    if not s: return 0
    try:
        if s.endswith("B"): return int(float(s[:-1]) * 1_000_000_000)
        if s.endswith("M"): return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"): return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except: return 0

def fetch_app_details_reliable(app_id: str) -> dict | None:
    """
    Fetch from google-play-scraper (3 regions) then cross-validate
    with direct HTML scrape. Returns the most accurate combined result.
    """
    best = None

    for lang, country in [("en","us"), ("en","gb"), ("en","au")]:
        try:
            d = gp_app(app_id, lang=lang, country=country)
            if d is None: continue
            sc = d.get("score")
            try: sc = float(sc) if sc else None
            except: sc = None
            if sc == 0.0: sc = None

            if best is None:
                best = dict(d)
                best["score"] = sc
            elif sc is not None and best.get("score") is None:
                best = dict(d)
                best["score"] = sc

            if best.get("score") is not None:
                break
        except Exception:
            time.sleep(0.3)
            continue

    if best is None:
        return None

    # Cross-validate with HTML scrape for accuracy
    try:
        html_data = _scrape_play_html(app_id)
        if html_data.get("score") and html_data["score"] > 0:
            best["score"] = html_data["score"]
        if html_data.get("minInstalls", 0) > 0:
            api_inst = _parse_installs(best.get("minInstalls") or 0)
            if api_inst == 0:
                best["minInstalls"] = html_data["minInstalls"]
        if html_data.get("developerEmail") and not best.get("developerEmail"):
            best["developerEmail"] = html_data["developerEmail"]
    except Exception:
        pass

    return best

# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD GENERATION
# ══════════════════════════════════════════════════════════════════════════════
KEYWORD_GENERATION_SYSTEM_PROMPT = """You are a Google Play Store keyword expert specializing in finding apps that need review management or reputation improvement services.

CONTEXT: We offer a Play Store review improvement service to app developers. We target apps that either:
- Have poor ratings (1.0-2.5 stars) and need reputation recovery (Hunter Mode)
- Are brand new with no ratings yet and need their first reviews (Normal Mode)

YOUR GOAL: Generate search keywords that find REAL apps in the SAME niche as the original keyword, where developers are likely to need and pay for review improvement services.

STRICT RULES:
- Stay in the EXACT same niche/industry as the original keyword
- Do NOT drift into tangentially related industries
- Keywords must be specific enough to find real apps on Play Store
- Focus on niches: fintech, productivity, business tools, health/fitness, education, food delivery, local services, e-commerce, utilities
- Each keyword should be a realistic 2-5 word Play Store search query
- Avoid single-word generic keywords

Return ONLY a valid JSON array of strings. No markdown, no explanation."""

def ai_gen_keywords(original: str, used: list) -> list:
    key = get_cfg("GROQ_API_KEY")
    if not key:
        push_log("GROQ_API_KEY not set — using fallback keywords")
        return []
    try:
        client = Groq(api_key=key)
        prompt = (
            f"Original keyword: '{original}'\n"
            f"Already used (do NOT repeat): {', '.join(used) if used else 'none'}\n\n"
            f"Generate exactly 10 NEW Google Play Store search keywords in the SAME niche as '{original}'.\n"
            f"Target small/indie apps that may need review improvement services.\n"
            f"Return ONLY a JSON array of 10 strings."
        )
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": KEYWORD_GENERATION_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt}
            ],
            temperature=0.6, max_tokens=400
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
# EMAIL TEMPLATE
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
    sender_name    = get_cfg("SENDER_NAME",    "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    return (tpl
        .replace("{{app_name}}",       lead.get("app_name",  ""))
        .replace("{{developer}}",      lead.get("developer", ""))
        .replace("{{category}}",       lead.get("category",  ""))
        .replace("{{installs}}",       str(lead.get("installs", "")))
        .replace("{{score}}",          str(lead.get("score", "") or "N/A"))
        .replace("{{url}}",            lead.get("url", ""))
        .replace("{{sender_name}}",    sender_name)
        .replace("{{sender_company}}", sender_company)
    )

def ai_gen_email(lead: dict, base_subject: str, base_body: str) -> tuple:
    """
    Fills placeholders only. Does NOT rewrite the email.
    AI only used for custom {{placeholders}} the user added.
    """
    subject = fill_template(base_subject, lead)
    body    = fill_template(base_body,    lead)

    remaining = re.findall(r"\{\{(\w+)\}\}", body + subject)
    key = get_cfg("GROQ_API_KEY")
    if remaining and key:
        sc           = lead.get("score")
        score_info   = f"{sc:.1f} stars" if sc else "no rating yet (brand new)"
        install_info = f"{lead['installs']:,} installs" if lead.get("installs") else "just launched"
        try:
            client = Groq(api_key=key)
            prompt = (
                f"Fill ONLY the {{{{placeholders}}}} in the template below.\n"
                f"Do NOT change any other word or sentence.\n"
                f"Placeholders to fill: {remaining}\n\n"
                f"APP: name={lead.get('app_name','')} | dev={lead.get('developer','')} | "
                f"cat={lead.get('category','')} | {install_info} | {score_info}\n\n"
                f"Subject: {subject}\n\nBody:\n{body}\n\n"
                f"Return ONLY JSON: {{\"subject\":\"...\",\"body\":\"...\"}}\n"
                f"Use \\n for line breaks in body."
            )
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, max_tokens=600
            )
            raw  = resp.choices[0].message.content.strip()
            raw  = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
            data = json.loads(raw)
            subject = data.get("subject") or subject
            body    = (data.get("body") or body).replace("\\n", "\n")
        except Exception as e:
            push_log(f"  AI placeholder fill error: {e}")

    return subject, body

def build_html_email(plain_body: str, lead: dict, unsubscribe_url: str = "") -> str:
    escaped   = plain_body.strip().replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    body_html = escaped.replace("\n", "<br>\n")
    unsub = ""
    if unsubscribe_url:
        unsub = (f'\n<br><br>\n<span style="font-size:11px;color:#999;">'
                 f'<a href="{unsubscribe_url}" style="color:#999;">Unsubscribe</a></span>')
    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
        f'<body style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:560px;">'
        f'{body_html}{unsub}</body></html>'
    )

# ══════════════════════════════════════════════════════════════════════════════
# LEAD FILTER
# ══════════════════════════════════════════════════════════════════════════════
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

SEARCH_COMBOS = [
    ("en","us"), ("en","gb"), ("en","au"), ("en","ca"),
    ("en","nz"), ("en","ie"), ("en","sg"), ("en","za"),
]

def extract_email(text: str) -> str:
    if not text: return ""
    m = EMAIL_RE.search(str(text))
    return m.group(0) if m else ""

def passes_filter(installs: int, score, hunter: dict) -> bool:
    """
    HUNTER MODE  → installs ≤ max_installs, score > 0 AND score ≤ max_score
    NORMAL MODE  → installs ≤ 10 000, score is None or 0  (brand-new apps)
    """
    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score")  or 2.5)
        if installs > max_inst:            return False
        if score is None or score == 0:    return False
        if score > max_score:              return False
        return True

    if installs > 10_000:                  return False
    if score is not None and score > 0:    return False
    return True

# ══════════════════════════════════════════════════════════════════════════════
# LEAD GENERATION — parallel, no proxies, fast, accurate
# ══════════════════════════════════════════════════════════════════════════════
DETAIL_WORKERS = 10   # parallel detail fetches per keyword

def _qualify_one(app_id: str, keyword: str, hunter: dict) -> dict | None:
    """
    Full qualification for one app_id.
    Returns lead dict or None. Thread-safe (reads globals, no writes).
    """
    try:
        details = fetch_app_details_reliable(app_id)
        if not details:
            return None

        installs = _parse_installs(details.get("minInstalls") or details.get("installs") or 0)
        score    = details.get("score")
        try:    score = float(score) if score else None
        except: score = None
        if score == 0.0: score = None

        if not passes_filter(installs, score, hunter):
            return None

        if not is_allowed_country(details):
            return None

        email = (
            extract_email(details.get("developerEmail", ""))
            or extract_email(details.get("privacyPolicy", ""))
            or extract_email(details.get("description",  ""))
            or extract_email(details.get("recentChanges",""))
        )
        if not email:
            return None

        email = email.lower().strip()

        # Email validation before accepting
        valid, reason = is_valid_email(email)
        if not valid:
            return None

        return {
            "app_id":      app_id,
            "app_name":    details.get("title",      ""),
            "developer":   details.get("developer",  ""),
            "email":       email,
            "category":    details.get("genre",      ""),
            "installs":    installs,
            "score":       score,
            "description": (details.get("description") or "")[:300],
            "url":         f"https://play.google.com/store/apps/details?id={app_id}",
            "icon":        details.get("icon",        ""),
            "keyword":     keyword,
            "scraped_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
            "email_sent":  False,
        }
    except Exception:
        return None


def scrape_keyword(keyword: str, hunter: dict = None) -> list:
    """
    1. Search across all SEARCH_COMBOS → collect unique candidate app IDs
    2. Sort candidates (lowest installs / no score first — best leads)
    3. Parallel detail fetch + qualify (DETAIL_WORKERS threads)
    4. Email-validate → add to leads
    """
    global global_seen_ids, global_seen_emails

    mode_label = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🔍 [{mode_label}] Scraping: '{keyword}'")

    # ── Step 1: collect candidates ────────────────────────────────────────────
    seen_in_search: set = set()
    candidates: list    = []   # (app_id, search_item)

    for lang, country in SEARCH_COMBOS:
        if stop_event.is_set():
            break
        results = []
        for attempt in range(3):
            try:
                results = search(keyword, lang=lang, country=country, n_hits=500)
                push_log(f"  [{country}] {len(results)} results")
                break
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ["429","403","rate","blocked","captcha"]):
                    wait = 20 * (attempt + 1)
                    push_log(f"  ⏳ Rate-limit ({country}), wait {wait}s")
                    time.sleep(wait)
                elif attempt == 2:
                    push_log(f"  Search error ({country}): {str(e)[:60]}")
                else:
                    time.sleep(random.uniform(2, 5))

        for item in results:
            aid = item.get("appId", "")
            if not aid or aid in seen_in_search or aid in global_seen_ids:
                continue
            if is_duplicate_in_sheet(aid, ""):
                global_seen_ids.add(aid)
                continue
            seen_in_search.add(aid)
            candidates.append((aid, item))

        time.sleep(random.uniform(0.3, 0.8))

    push_log(f"  📋 {len(candidates)} unique candidates")
    if not candidates:
        sheet_log_keyword(keyword, 0)
        return []

    # ── Step 2: sort — lowest installs / no score first ───────────────────────
    def _sort_key(pair):
        _, item = pair
        sc   = item.get("score") or 0
        try: sc = float(sc)
        except: sc = 0.0
        inst = _parse_installs(item.get("minInstalls") or item.get("installs") or 0)
        has_score = 0 if sc == 0 else 1
        return (has_score, inst)

    candidates.sort(key=_sort_key)

    # ── Step 3: parallel detail fetch in batches ──────────────────────────────
    leads  = []
    BATCH  = 30

    for batch_start in range(0, len(candidates), BATCH):
        if stop_event.is_set():
            break

        batch = [aid for aid, _ in candidates[batch_start: batch_start + BATCH]
                 if aid not in global_seen_ids]

        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
            futures = {ex.submit(_qualify_one, aid, keyword, hunter): aid for aid in batch}

            for fut in as_completed(futures):
                aid  = futures[fut]
                lead = None
                try:
                    lead = fut.result()
                except Exception:
                    pass

                global_seen_ids.add(aid)

                if not lead:
                    continue

                email = lead["email"]

                # Thread-safe final dedup
                if email in global_seen_emails or is_duplicate_in_sheet("", email):
                    push_log(f"  ⏭️  Dup email: {email}")
                    continue

                leads.append(lead)
                global_seen_emails.add(email)
                register_in_sheet_memory(aid, email)

                sc_str = f"{lead['score']:.1f}★" if lead["score"] else "new"
                push_log(
                    f"  ✅ [{mode_label}] {lead['app_name']} | "
                    f"{lead['installs']:,} installs | {sc_str} | {email}"
                )

        push_log(f"  Batch {batch_start//BATCH+1} done | leads so far: {len(leads)}")
        time.sleep(random.uniform(0.4, 1.0))

    push_log(f"  📦 {len(leads)} new leads from '{keyword}'")
    sheet_log_keyword(keyword, len(leads))
    return leads

# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SEND
# ══════════════════════════════════════════════════════════════════════════════
def send_email(lead: dict, subject: str, body: str) -> bool:
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url or not lead.get("email"):
        push_log("EMAIL_SCRIPT_URL not set or no email")
        return False

    # Final gate — re-validate before send
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

# ══════════════════════════════════════════════════════════════════════════════
# MASTER AUTOMATION
# ══════════════════════════════════════════════════════════════════════════════
def run_automation(initial_kw: str, target: int, hunter: dict = None):
    global global_seen_ids, global_seen_emails

    upd(running=True, phase="loading_sheet", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode}")

    push_log("📋 Loading existing sheet records …")
    load_sheet_memory()
    push_log(f"   Memory: {len(sheet_memory_ids)} IDs, {len(sheet_memory_emails)} emails")

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

        # Add to sheet immediately after email validation (already validated in scrape_keyword)
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

        push_log(f"  🤖 Personalizing email for {lead['app_name']} …")
        subject, body = ai_gen_email(lead, base_subject, base_body)

        ok = send_email(lead, subject, body)
        lead["email_sent"] = ok
        with state_lock:
            if ok: state["emails_sent"] += 1
            state["leads"] = [l.copy() for l in all_leads]

        if ok:
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])

        if i < len(all_leads) - 1:
            wait = random.uniform(60, 120)
            push_log(f"  ⏳ {wait:.0f}s wait … ({i+1}/{len(all_leads)})")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
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
        push_log(f"  🤖 Personalizing email for {lead.get('app_name','')} …")
        subject, body = ai_gen_email(lead, base_subject, base_body)
        ok = send_email(lead, subject, body)
        if ok:
            sent += 1
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])
            with state_lock:
                state["emails_sent"] = state.get("emails_sent", 0) + 1
        if i < len(leads) - 1:
            wait = random.uniform(60, 120)
            push_log(f"  ⏳ {wait:.0f}s … ({i+1}/{len(leads)})")
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
        "GROQ_API_KEY":        data.get("groq_key")         or os.environ.get("GROQ_API_KEY",        ""),
        "APPS_SCRIPT_WEB_URL": data.get("sheet_url")        or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "EMAIL_SCRIPT_URL":    data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL",    ""),
        "SENDER_NAME":         data.get("sender_name")      or os.environ.get("SENDER_NAME",         ""),
        "SENDER_COMPANY":      data.get("sender_company")   or os.environ.get("SENDER_COMPANY",      ""),
        "EMAIL_SUBJECT":       data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT",       ""),
        "EMAIL_BODY":          data.get("email_body")       or os.environ.get("EMAIL_BODY",          ""),
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
    global_seen_ids     = set()
    global_seen_emails  = set()
    sheet_memory_ids    = set()
    sheet_memory_emails = set()
    sheet_memory_loaded = False
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
        "GROQ_API_KEY":        data.get("groq_key")         or os.environ.get("GROQ_API_KEY",        ""),
        "EMAIL_SCRIPT_URL":    data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL",    ""),
        "SENDER_NAME":         data.get("sender_name")      or os.environ.get("SENDER_NAME",         ""),
        "SENDER_COMPANY":      data.get("sender_company")   or os.environ.get("SENDER_COMPANY",      ""),
        "EMAIL_SUBJECT":       data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT",       ""),
        "EMAIL_BODY":          data.get("email_body")       or os.environ.get("EMAIL_BODY",          ""),
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
        "GROQ_API_KEY":     data.get("groq_key")         or os.environ.get("GROQ_API_KEY",     ""),
        "EMAIL_SCRIPT_URL": data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":      data.get("sender_name")      or os.environ.get("SENDER_NAME",      ""),
        "SENDER_COMPANY":   data.get("sender_company")   or os.environ.get("SENDER_COMPANY",   ""),
        "EMAIL_SUBJECT":    data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT",     ""),
        "EMAIL_BODY":       data.get("email_body")       or os.environ.get("EMAIL_BODY",        ""),
    }
    sample = {
        "app_name":  data.get("sample_app_name",  "MyApp Pro"),
        "developer": data.get("sample_developer", "John Dev"),
        "category":  "Productivity",
        "installs":  1500,
        "score":     data.get("sample_score", 2.1),
        "email":     test_to,
        "url":       "https://play.google.com/store/apps/details?id=com.example",
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
    data      = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400
    try:
        r = requests.post(sheet_url, json={"action": "get_pending"}, timeout=20)
        result = r.json() if r.text else {}
        leads  = result.get("leads", [])
        return jsonify({"ok": True, "count": len(leads), "leads": leads})
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
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
