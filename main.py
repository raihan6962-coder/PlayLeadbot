"""
PlayLead Engine — Production Build v4
=======================================
- Lead fix: expanded filters to catch trust/review-needing apps
- Keywords: targets apps where trust & reviews matter
- Email: plain-text style HTML, minimal styling, small unsubscribe
- Sender: PlayReview hardcoded
"""

import os, time, random, threading, json, re, logging, socket, hashlib
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from google_play_scraper import search, app as gp_app
from groq import Groq
import requests

application = Flask(__name__, static_folder=".")
app = application
CORS(application)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

stop_event   = threading.Event()
state_lock   = threading.Lock()
state = {
    "running": False, "phase": "idle", "keyword": "",
    "keywords_used": [], "leads_found": 0, "emails_sent": 0,
    "logs": [], "leads": [], "email_script_stats": [],
}

global_seen_ids:     set  = set()
global_seen_emails:  set  = set()
sheet_memory_ids:    set  = set()
sheet_memory_emails: set  = set()
sheet_memory_loaded: bool = False
sheet_memory_lock = threading.Lock()
run_cfg = {}

def get_cfg(key, fallback=""):
    return run_cfg.get(key) or os.environ.get(key, fallback)

def push_log(msg):
    with state_lock:
        state["logs"].append({"time": time.strftime("%H:%M:%S"), "msg": msg})
        if len(state["logs"]) > 500:
            state["logs"] = state["logs"][-500:]
    log.info(msg)

def upd(**kw):
    with state_lock:
        state.update(kw)


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SCRIPT ROTATION — multiple Apps Script URLs, one per line
# ══════════════════════════════════════════════════════════════════════════════
_email_scripts:      list = []
_email_script_lock        = threading.Lock()
_script_fail_counts: dict = {}
_script_sent_counts: dict = {}
_current_script_idx: int  = 0
MAX_SCRIPT_FAILS           = 3
MAX_SENDS_PER_SCRIPT       = 80


# Maps each email script URL → a dedicated proxy IP for sending
_script_proxy_map: dict = {}   # url -> proxy_dict or None
_script_proxy_lock = threading.Lock()


def _assign_proxies_to_scripts(urls: list):
    """Give each script URL a dedicated proxy from the pool (random assignment)."""
    global _script_proxy_map
    with _proxy_lock:
        available = list(_proxy_pool)
    with _script_proxy_lock:
        _script_proxy_map = {}
        if not available:
            for u in urls:
                _script_proxy_map[u] = None   # direct — no proxy
            push_log("⚠️  No proxies — scripts will send direct")
            return
        random.shuffle(available)
        for i, u in enumerate(urls):
            proxy_url = available[i % len(available)]
            _script_proxy_map[u] = {"http": proxy_url, "https": proxy_url}
        push_log(f"🔗 Assigned proxies: {len(urls)} scripts → {min(len(urls), len(available))} unique IPs")


def _get_script_proxy(script_url: str):
    """Return the dedicated proxy for this script URL."""
    with _script_proxy_lock:
        return _script_proxy_map.get(script_url)


def _load_email_scripts():
    global _email_scripts, _current_script_idx, _script_fail_counts, _script_sent_counts
    raw = get_cfg("EMAIL_SCRIPT_URLS", "") or get_cfg("EMAIL_SCRIPT_URL", "")
    urls = [u.strip() for u in raw.split("\n") if u.strip().startswith("http")]
    with _email_script_lock:
        _email_scripts      = urls
        _current_script_idx = 0
        _script_fail_counts = {u: 0 for u in urls}
        _script_sent_counts = {u: 0 for u in urls}
    _assign_proxies_to_scripts(urls)
    _refresh_script_stats()
    if urls:
        push_log(f"📧 {len(urls)} email script URL(s) loaded")
    return urls


def _get_active_script():
    with _email_script_lock:
        if not _email_scripts:
            return ""
        start = _current_script_idx % len(_email_scripts)
        for offset in range(len(_email_scripts)):
            idx = (start + offset) % len(_email_scripts)
            url = _email_scripts[idx]
            if (_script_fail_counts.get(url, 0) < MAX_SCRIPT_FAILS and
                    _script_sent_counts.get(url, 0) < MAX_SENDS_PER_SCRIPT):
                return url
        # all exhausted — reset
        push_log("⚠️  All email scripts hit limit — resetting")
        for u in _email_scripts:
            _script_fail_counts[u] = 0
            _script_sent_counts[u] = 0
        return _email_scripts[0]


def _mark_script_ok(url):
    global _current_script_idx
    with _email_script_lock:
        _script_fail_counts[url] = 0
        _script_sent_counts[url] = _script_sent_counts.get(url, 0) + 1
        sent = _script_sent_counts[url]
        if sent >= MAX_SENDS_PER_SCRIPT and url in _email_scripts:
            idx = _email_scripts.index(url)
            _current_script_idx = (idx + 1) % len(_email_scripts)
            push_log(f"  🔄 Script #{idx+1} reached {sent} sends — rotating to #{_current_script_idx+1}")
    _refresh_script_stats()


def _mark_script_failed(url):
    global _current_script_idx
    with _email_script_lock:
        _script_fail_counts[url] = _script_fail_counts.get(url, 0) + 1
        if _script_fail_counts[url] >= MAX_SCRIPT_FAILS and url in _email_scripts:
            idx = _email_scripts.index(url)
            _current_script_idx = (idx + 1) % len(_email_scripts)
            push_log(f"  🔄 Script #{idx+1} failed — rotating to #{_current_script_idx+1}")
    _refresh_script_stats()


def _refresh_script_stats():
    with _email_script_lock:
        stats = []
        cur = _current_script_idx % max(len(_email_scripts), 1)
        for i, u in enumerate(_email_scripts):
            s = "quota"  if _script_sent_counts.get(u, 0) >= MAX_SENDS_PER_SCRIPT else \
                "retired" if _script_fail_counts.get(u, 0) >= MAX_SCRIPT_FAILS else "active"
            stats.append({"index": i+1, "url": u[:65]+"…" if len(u)>65 else u,
                           "sent": _script_sent_counts.get(u,0), "fails": _script_fail_counts.get(u,0),
                           "active": i == cur, "status": s})
    with state_lock:
        state["email_script_stats"] = stats


# ══════════════════════════════════════════════════════════════════════════════
# PROXY POOL
# ══════════════════════════════════════════════════════════════════════════════
_proxy_pool:        list = []
_proxy_lock              = threading.Lock()
_proxy_fail_counts: dict = {}
MAX_PROXY_FAILS           = 3


def _load_proxy_pool():
    global _proxy_pool
    raw = get_cfg("PROXY_LIST", "")
    proxies = [p.strip() for p in raw.split("\n")
               if p.strip().startswith("http") or p.strip().startswith("socks")]
    with _proxy_lock:
        _proxy_pool = proxies
        _proxy_fail_counts.clear()
    if proxies:
        push_log(f"🔄 {len(proxies)} proxies loaded")
    return proxies


def _get_next_proxy():
    sa = get_cfg("SCRAPER_API_KEY", "")
    if sa:
        u = f"http://scraperapi:{sa}@proxy-server.scraperapi.com:8001"
        return {"http": u, "https": u}
    with _proxy_lock:
        ok = [p for p in _proxy_pool if _proxy_fail_counts.get(p, 0) < MAX_PROXY_FAILS]
        if not ok:
            if _proxy_pool:
                _proxy_fail_counts.clear()
                ok = list(_proxy_pool)
            else:
                return None
        c = random.choice(ok)
    return {"http": c, "https": c}


def _mark_proxy_failed(pd):
    if not pd: return
    u = pd.get("http") or pd.get("https")
    if u:
        with _proxy_lock:
            _proxy_fail_counts[u] = _proxy_fail_counts.get(u, 0) + 1


def _mark_proxy_ok(pd):
    if not pd: return
    u = pd.get("http") or pd.get("https")
    if u:
        with _proxy_lock:
            _proxy_fail_counts[u] = 0


def robust_post(url, timeout=20, retries=3, **kwargs):
    """General POST with rotating proxy (for sheet/scraping)."""
    for attempt in range(retries):
        proxy = _get_next_proxy()
        try:
            r = requests.post(url, proxies=proxy, timeout=timeout, **kwargs)
            _mark_proxy_ok(proxy)
            return r
        except Exception:
            _mark_proxy_failed(proxy)
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
    return None


def _post_with_proxy(url, proxy, timeout=30, retries=2, **kwargs):
    """POST using a FIXED dedicated proxy (for email scripts — same IP per Gmail account)."""
    for attempt in range(retries):
        try:
            r = requests.post(url, proxies=proxy, timeout=timeout, **kwargs)
            return r
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(3 + random.uniform(0, 2))
    # Fallback: try direct if proxy failed
    try:
        return requests.post(url, timeout=timeout, **kwargs)
    except:
        return None


def _play_search_with_proxy(keyword, lang, country, n_hits):
    proxy = _get_next_proxy()
    orig  = requests.get
    if proxy:
        def patched(*a, **kw):
            kw.setdefault("proxies", proxy); kw.setdefault("timeout", 25)
            try:    r = orig(*a, **kw); _mark_proxy_ok(proxy); return r
            except: _mark_proxy_failed(proxy); raise
        requests.get = patched
    try:    return search(keyword, lang=lang, country=country, n_hits=n_hits)
    except: _mark_proxy_failed(proxy); raise
    finally:
        if proxy: requests.get = orig


def _play_app_with_proxy(app_id, lang, country):
    proxy = _get_next_proxy()
    orig  = requests.get
    if proxy:
        def patched(*a, **kw):
            kw.setdefault("proxies", proxy); kw.setdefault("timeout", 25)
            try:    r = orig(*a, **kw); _mark_proxy_ok(proxy); return r
            except: _mark_proxy_failed(proxy); raise
        requests.get = patched
    try:    return gp_app(app_id, lang=lang, country=country)
    except: _mark_proxy_failed(proxy); raise
    finally:
        if proxy: requests.get = orig


# ── Country filter ────────────────────────────────────────────────────────────
BLOCKED_COUNTRIES = {
    "BD","IN","PK","NG","GH","KE","TZ","UG","ET","EG","MA","TN","DZ","LY",
    "SD","SO","AO","MZ","ZM","ZW","MW","RW","SN","CI","CM","CD","MG","MM",
    "KH","LA","NP","LK","AF","IQ","SY","YE","LB","JO","PS","PH","ID","VN","TH","MY",
}
BLOCKED_ADDR_KW = [
    "bangladesh","dhaka","india","mumbai","delhi","bangalore","pakistan","karachi",
    "nigeria","lagos","kenya","nairobi","ghana","indonesia","jakarta",
    "philippines","vietnam","myanmar","nepal","sri lanka","ethiopia","egypt",
    "morocco","tanzania","uganda",
]

def is_allowed_country(d):
    cc = (d.get("developerCountry") or d.get("country") or "").upper()
    if cc and cc in BLOCKED_COUNTRIES: return False
    addr = (d.get("developerAddress") or "").lower()
    return not any(k in addr for k in BLOCKED_ADDR_KW)


# ── Google Sheet helpers ──────────────────────────────────────────────────────
def sheet_post(payload):
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url: return None
    try:
        r = robust_post(url, json=payload, timeout=15)
        return r.json() if (r and r.text) else {}
    except Exception as e:
        push_log(f"  Sheet error: {e}"); return None

def sheet_append_lead(lead):
    sheet_post({"action":"append","tab":"All Leads","row":{
        "App Name":lead["app_name"],"Developer":lead["developer"],
        "Email":lead["email"],"Category":lead["category"],
        "Installs":lead["installs"],"Score":lead["score"] or "",
        "URL":lead["url"],"Keyword":lead["keyword"],
        "Scraped At":lead["scraped_at"],"Email Sent":"No","App ID":lead["app_id"],
    }})

def sheet_append_qualified(lead):
    sheet_post({"action":"append","tab":"Qualified Leads","row":{
        "App Name":lead["app_name"],"Developer":lead["developer"],
        "Email":lead["email"],"Category":lead["category"],
        "Installs":lead["installs"],"Score":lead["score"] or "",
        "URL":lead["url"],"Keyword":lead["keyword"],
        "Scraped At":lead["scraped_at"],"Email Sent":"Pending","App ID":lead["app_id"],
    }})

def sheet_mark_sent(app_id, email, app_name):
    sheet_post({"action":"mark_sent","app_id":app_id})
    sheet_post({"action":"append","tab":"Email Sent","row":{
        "App ID":app_id,"App Name":app_name,
        "Email":email,"Sent At":time.strftime("%Y-%m-%d %H:%M:%S"),
    }})

def sheet_log_keyword(keyword, count):
    sheet_post({"action":"append","tab":"Keyword Log","row":{
        "Keyword":keyword,"Leads Found":count,
        "Logged At":time.strftime("%Y-%m-%d %H:%M:%S"),
    }})


# ── Sheet memory (dedup) ──────────────────────────────────────────────────────
def load_sheet_memory():
    global sheet_memory_ids, sheet_memory_emails, sheet_memory_loaded
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        push_log("⚠️  No sheet URL — dedup disabled")
        with sheet_memory_lock: sheet_memory_loaded = True
        return
    push_log("📋 Loading sheet memory …")
    try:
        r = robust_post(url, json={"action":"get_all","tab":"All Leads"}, timeout=30)
        result   = r.json() if (r and r.text) else {}
        records  = result.get("records", [])
        ids, ems = set(), set()
        for rec in records:
            if rec.get("App ID"): ids.add(rec["App ID"].strip())
            if rec.get("Email"):  ems.add(rec["Email"].strip().lower())
        with sheet_memory_lock:
            sheet_memory_ids    = ids
            sheet_memory_emails = ems
            sheet_memory_loaded = True
        push_log(f"✅ Sheet memory: {len(ids)} apps, {len(ems)} emails")
    except Exception as e:
        push_log(f"⚠️  Sheet load failed: {e}")
        with sheet_memory_lock: sheet_memory_loaded = True

def is_dup(app_id, email):
    with sheet_memory_lock:
        if app_id and app_id in sheet_memory_ids:       return True
        if email   and email.lower() in sheet_memory_emails: return True
    return False

def register(app_id, email):
    with sheet_memory_lock:
        if app_id: sheet_memory_ids.add(app_id)
        if email:  sheet_memory_emails.add(email.lower())


# ── App detail fetch ──────────────────────────────────────────────────────────
DETAIL_COMBOS = [("en","us"),("en","gb"),("en","au")]

def fetch_details(app_id):
    first = None
    for lang, country in DETAIL_COMBOS:
        try:
            d = _play_app_with_proxy(app_id, lang, country)
            if first is None: first = d
            if d.get("score") and d["score"] > 0: return d
        except Exception:
            time.sleep(random.uniform(1, 2))
    return first


# ── Email validation ──────────────────────────────────────────────────────────
DISPOSABLE = {
    "mailinator.com","guerrillamail.com","10minutemail.com","trashmail.com",
    "yopmail.com","throwam.com","sharklasers.com","spam4.me","tempmail.com",
    "fakeinbox.com","maildrop.cc","dispostable.com","mailnull.com",
    "spamgourmet.com","discard.email","getnada.com","tempr.email",
}
EMAIL_RE   = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
SYNTAX_RE  = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def valid_email(email):
    if not email: return False, "empty"
    email = email.strip().lower()
    if not SYNTAX_RE.match(email): return False, "bad_syntax"
    domain = email.split("@")[-1]
    if domain in DISPOSABLE: return False, "disposable"
    try:
        socket.setdefaulttimeout(4)
        socket.getaddrinfo(domain, None)
    except: return False, "no_dns"
    return True, "ok"

def extract_email(text):
    if not text: return ""
    m = EMAIL_RE.search(str(text))
    return m.group(0) if m else ""


# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD GENERATION — targets trust/review-critical app categories
# ══════════════════════════════════════════════════════════════════════════════
KW_SYSTEM = """You are a Google Play Store keyword specialist.

Your job: given a seed keyword, generate CLOSELY RELATED search terms that find apps
in the EXACT SAME niche — apps where user TRUST and REVIEWS directly affect installs.

STRICT RULES:
1. Stay in the EXACT same niche as the seed. If seed = "crypto wallet", generate:
   crypto wallet app, bitcoin wallet android, ethereum wallet mobile, crypto exchange app, etc.
   NOT: crypto calculator, crypto tracker, crypto news — these don't need trust/reviews.

2. Only generate keywords for HIGH-TRUST niches where reviews matter:
   - Fintech/payments/lending/crypto (users check reviews before giving money/data)
   - E-commerce/marketplace/shopping (reviews drive purchases)
   - Healthcare/medical/mental health (users research carefully)
   - Education/kids apps (parents vet thoroughly)
   - Delivery/booking/service apps (reliability = reviews)
   - Dating/social (safety concerns)
   - Business/B2B SaaS (buyers read reviews)

3. REJECT utility/tool keywords that don't need trust:
   - calculators, converters, managers, trackers, widgets, utilities
   - These apps don't need review services

4. Keywords must be real Play Store search queries (2-5 words).

Return ONLY a JSON array of strings. No markdown, no explanation."""

def ai_gen_keywords(original, used):
    key = get_cfg("GROQ_API_KEY")
    if not key: return []
    try:
        client = Groq(api_key=key)
        prompt = (
            f"Seed keyword: '{original}'\n"
            f"Already used (skip these): {', '.join(used[-20:]) if used else 'none'}\n\n"
            f"Generate 12 NEW Google Play search keywords that are CLOSELY related to '{original}'.\n"
            f"Rules:\n"
            f"- Same niche ONLY — if seed is 'crypto wallet', generate wallet/exchange/trading keywords\n"
            f"- Only apps where trust/reviews DIRECTLY affect whether users install\n"
            f"- NO utility apps: no calculators, converters, managers, trackers, widgets\n"
            f"- Real Play Store search terms, 2-5 words each\n"
            f"- All 12 must be different from already used list\n"
            f"Return ONLY a JSON array of 12 strings."
        )
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role":"system","content":KW_SYSTEM},
                {"role":"user","content":prompt}
            ],
            temperature=0.75, max_tokens=500
        )
        raw  = resp.choices[0].message.content.strip()
        raw  = re.sub(r"```[a-z]*","",raw).replace("```","").strip()
        kws  = json.loads(raw)
        valid = [k for k in kws if isinstance(k,str) and k.strip() and k not in used]
        push_log(f"🤖 AI keywords ({len(valid)}): {valid[:6]}…")
        return valid
    except Exception as e:
        push_log(f"AI keyword error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# FILTER — who qualifies as a lead
# ══════════════════════════════════════════════════════════════════════════════
"""
NORMAL MODE — 3 types of qualifying apps:
  Type A: New app, 0 installs, no rating yet        → needs first reviews
  Type B: Small app ≤50K installs, rating ≤3.5★     → needs rating improvement
  Type C: Medium app ≤100K installs, rating ≤2.5★   → urgently needs help

HUNTER MODE — user-configured thresholds.

This catches FAR more leads than before (was: ≤10K installs + NO rating at all).
"""

def passes_filter(installs, score, hunter):
    """
    HUNTER MODE — apps WITH a rating, within user-set thresholds.
                  No rating = rejected (hunter targets rated apps only).
    NORMAL MODE — ONLY brand new apps with NO rating yet.
                  Apps with any rating = rejected.
    """
    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 50000)
        max_score = float(hunter.get("max_score") or 3.5)
        # Must HAVE a real rating — hunter targets rated apps
        if score is None or score == 0:
            return False
        if installs > max_inst:
            return False
        if score > max_score:
            return False
        return True

    # NORMAL MODE — only brand new apps, no rating at all
    if score is not None and score > 0:
        return False          # has rating → skip in normal mode
    if installs > 10000:
        return False          # too big for a "new" app
    return True


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL TEMPLATE — plain text style, minimal HTML, small unsubscribe
# ══════════════════════════════════════════════════════════════════════════════
SENDER_NAME    = "PlayReview"   # hardcoded
SENDER_COMPANY = "PlayReview"

DEFAULT_EMAIL_SUBJECT = "Your {{app_name}} reviews on Google Play"

DEFAULT_EMAIL_BODY = """Hi {{developer}},

I noticed {{app_name}} on Google Play{{score_line}}.

Apps in your category live or die by their Play Store rating — even a 0.5 star improvement can double install rates.

We help app developers clean up their Play Store presence: removing unfair reviews, building genuine social proof, and protecting their rating long-term.

If you're open to a quick 10-minute call this week, I'd love to show you what we've done for similar apps.

Best,
PlayReview"""


def build_html_email(plain_body, lead, unsubscribe_url=""):
    """
    Minimal HTML — looks like a real personal email, not a marketing blast.
    No heavy styling, no colored headers, no image badges.
    Just clean text with a tiny unsubscribe link at the bottom.
    """
    # Convert plain text to basic HTML paragraphs
    lines = plain_body.strip().split("\n")
    html_lines = []
    for line in lines:
        line = line.strip()
        if line:
            html_lines.append(
                f'<p style="margin:0 0 12px;font-size:14px;line-height:1.6;color:#222;">{line}</p>'
            )
        else:
            html_lines.append('<br>')
    body_html = "\n".join(html_lines)

    unsub = ""
    if unsubscribe_url:
        unsub = (
            f'<p style="margin:32px 0 0;font-size:11px;color:#aaa;border-top:1px solid #eee;'
            f'padding-top:12px;text-align:center;">'
            f'<a href="{unsubscribe_url}" style="color:#aaa;text-decoration:underline;">Unsubscribe</a>'
            f'</p>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;font-family:Arial,sans-serif;background:#fff;max-width:580px;">
{body_html}
{unsub}
</body></html>"""


def fill_template(tpl, lead):
    score = lead.get("score")
    if score and score > 0:
        score_line = f" — currently rated {score:.1f}★"
    else:
        score_line = " (just launched, building reviews)"

    return (tpl
        .replace("{{app_name}}",    lead.get("app_name",""))
        .replace("{{developer}}",   lead.get("developer",""))
        .replace("{{category}}",    lead.get("category",""))
        .replace("{{installs}}",    str(lead.get("installs","")))
        .replace("{{score}}",       str(score or "N/A"))
        .replace("{{score_line}}",  score_line)
        .replace("{{url}}",         lead.get("url",""))
        .replace("{{sender_name}}", SENDER_NAME)
    )


def ai_gen_email(lead, base_subject, base_body):
    key = get_cfg("GROQ_API_KEY")
    if not key:
        return fill_template(base_subject, lead), fill_template(base_body, lead)

    score = lead.get("score")
    score_info   = f"{score:.1f} stars" if score else "no rating yet (brand new app)"
    install_info = f"{lead['installs']:,} installs" if lead.get("installs") else "just launched"

    try:
        client = Groq(api_key=key)
        prompt = f"""Personalise this cold email for a specific app developer.
Write like a REAL person, not a marketing robot. Short, direct, personal.

BASE TEMPLATE:
Subject: {base_subject}
Body:
{base_body}

APP INFO:
- App: {lead.get('app_name','')}
- Developer: {lead.get('developer','')}
- Category: {lead.get('category','')}
- Installs: {install_info}
- Rating: {score_info}

RULES:
- Keep it short (max 120 words in body)
- Sound like a real person reached out personally
- Mention the app name and rating/status naturally
- NO marketing buzzwords, NO exclamation marks
- Plain conversational English
- Return ONLY JSON: {{"subject":"...","body":"..."}}
- Use \\n for line breaks in body"""

        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"user","content":prompt}],
            temperature=0.4, max_tokens=400
        )
        raw  = resp.choices[0].message.content.strip()
        raw  = re.sub(r"```[a-z]*","",raw).replace("```","").strip()
        data = json.loads(raw)
        subj = data.get("subject") or fill_template(base_subject, lead)
        body = data.get("body")    or fill_template(base_body, lead)
        return subj, body.replace("\\n","\n")
    except Exception as e:
        push_log(f"  AI email fallback: {e}")
        return fill_template(base_subject, lead), fill_template(base_body, lead)


# ── Unsubscribe token ─────────────────────────────────────────────────────────
def _unsub_token(email):
    salt = os.environ.get("UNSUB_SALT","playleadbot-2024")
    return hashlib.sha256(f"{salt}:{email.lower()}".encode()).hexdigest()[:32]


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPER — with expanded filter
# ══════════════════════════════════════════════════════════════════════════════
ALL_COMBOS = [
    ("en","us"),("en","gb"),("en","au"),("en","ca"),
    ("en","sg"),("en","nz"),("en","ie"),("en","za"),
    ("en","us"),("en","gb"),   # duplicates to increase result volume
]
MIN_LEADS_PER_KW = 2


def _quick_filter_from_search(item, hunter):
    """
    Fast pre-filter using search result data only (no extra API call).
    Eliminates obvious rejects before expensive detail fetch.
    """
    installs = item.get("minInstalls") or 0
    score    = item.get("score") or None
    if score == 0.0: score = None

    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 50000)
        max_score = float(hunter.get("max_score") or 3.5)
        if score is None or score == 0: return False   # hunter needs rating
        if installs > max_inst:         return False
        if score > max_score:           return False
        return True

    # Normal mode: only new apps with no rating
    if score is not None and score > 0: return False
    if installs > 10000:                return False
    return True


def _process_one_app(item, hunter, keyword):
    """
    Fetch details + validate for a single app.
    Returns a lead dict or None.
    """
    global global_seen_ids, global_seen_emails

    app_id = item.get("appId", "")
    if not app_id: return None

    # Check dedup before slow API call
    if app_id in global_seen_ids:       return None
    if is_dup(app_id, ""):
        global_seen_ids.add(app_id)
        return None

    # Fetch full details (needed for email + country + description)
    details = fetch_details(app_id)
    if not details:
        global_seen_ids.add(app_id)
        return None

    installs = details.get("minInstalls") or 0
    score    = details.get("score") or None
    if score == 0.0: score = None

    # Re-check filter with accurate data
    if not passes_filter(installs, score, hunter):
        global_seen_ids.add(app_id)
        return None

    if not is_allowed_country(details):
        global_seen_ids.add(app_id)
        return None

    # Extract email from all available fields
    email = (
        extract_email(details.get("developerEmail", ""))
        or extract_email(details.get("privacyPolicy", ""))
        or extract_email(details.get("description", ""))
        or extract_email(details.get("recentChanges", ""))
    )
    if not email:
        global_seen_ids.add(app_id)
        return None

    email = email.lower().strip()
    ok, reason = valid_email(email)
    if not ok:
        global_seen_ids.add(app_id)
        return None

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
        "description": (details.get("description") or "")[:250],
        "url":         f"https://play.google.com/store/apps/details?id={app_id}",
        "icon":        details.get("icon", ""),
        "keyword":     keyword,
        "scraped_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "email_sent":  False,
    }

    # Register immediately (thread-safe via GIL on dict ops)
    global_seen_ids.add(app_id)
    global_seen_emails.add(email)
    register(app_id, email)

    s_str = f"{score:.1f}★" if score else "new"
    push_log(f"  ✅ {lead['app_name']} | {installs:,} | {s_str} | {email}")
    return lead


def scrape_keyword(keyword, hunter=None, min_leads=MIN_LEADS_PER_KW):
    """
    FAST scraper — 3 speed improvements:
    1. Pre-filter from search results (no API call) — skip obvious rejects
    2. Parallel detail fetch — 8 threads simultaneously
    3. Search ALL combos in parallel first, then process results
    """
    global global_seen_ids, global_seen_emails
    from concurrent.futures import ThreadPoolExecutor, as_completed

    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🔍 [{mode}] '{keyword}' — parallel scrape starting …")
    leads       = []
    leads_lock  = threading.Lock()

    # ── Step 1: Collect candidates from multiple regions in parallel ──────────
    combos = list(ALL_COMBOS)
    random.shuffle(combos)
    all_candidates = {}   # app_id -> item dict (deduped)

    def search_one_region(lang, country):
        for attempt in range(3):
            try:
                results = _play_search_with_proxy(keyword, lang=lang, country=country, n_hits=250)
                return country, results
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ["429","403","rate","blocked","captcha"]):
                    wait = 15*(attempt+1) + random.uniform(3,8)
                    push_log(f"  🚦 Rate-limit ({country}) — {wait:.0f}s …")
                    time.sleep(wait)
                elif attempt == 2:
                    return country, []
                else:
                    time.sleep(random.uniform(1,3))
        return country, []

    # Search up to 4 regions simultaneously
    search_combos = combos[:4]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(search_one_region, lang, country): (lang, country)
                for lang, country in search_combos}
        for fut in as_completed(futs):
            if stop_event.is_set(): break
            country, results = fut.result()
            before = len(all_candidates)
            for item in results:
                aid = item.get("appId","")
                if aid and aid not in all_candidates and aid not in global_seen_ids:
                    # Quick pre-filter — no API call yet
                    if _quick_filter_from_search(item, hunter):
                        all_candidates[aid] = item
            push_log(f"  [{country}] {len(results)} results → {len(all_candidates)-before} candidates")

    if not all_candidates:
        push_log(f"  📦 '{keyword}' → 0 leads (no candidates)")
        sheet_log_keyword(keyword, 0)
        return []

    push_log(f"  🎯 {len(all_candidates)} candidates → fetching details (8 threads) …")

    # ── Step 2: Parallel detail fetch + validation ────────────────────────────
    candidate_list = list(all_candidates.values())
    random.shuffle(candidate_list)

    def process_batch(item):
        if stop_event.is_set():                    return None
        if len(leads) >= min_leads * 6:            return None  # enough found
        return _process_one_app(item, hunter, keyword)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(process_batch, item) for item in candidate_list]
        for fut in as_completed(futs):
            if stop_event.is_set(): break
            lead = fut.result()
            if lead:
                with leads_lock:
                    leads.append(lead)

    # ── Step 3: If still not enough, search more regions ─────────────────────
    if len(leads) < min_leads and not stop_event.is_set():
        push_log(f"  🔄 Only {len(leads)}/{min_leads} — searching extra regions …")
        extra_combos = combos[4:]
        for lang, country in extra_combos:
            if stop_event.is_set() or len(leads) >= min_leads: break
            try:
                results = _play_search_with_proxy(keyword, lang=lang, country=country, n_hits=250)
                extra_candidates = {}
                for item in results:
                    aid = item.get("appId","")
                    if aid and aid not in global_seen_ids and aid not in all_candidates:
                        if _quick_filter_from_search(item, hunter):
                            extra_candidates[aid] = item

                push_log(f"  [{country}] {len(extra_candidates)} new candidates")
                with ThreadPoolExecutor(max_workers=6) as ex:
                    futs = [ex.submit(_process_one_app, item, hunter, keyword)
                            for item in extra_candidates.values()]
                    for fut in as_completed(futs):
                        if stop_event.is_set(): break
                        lead = fut.result()
                        if lead:
                            with leads_lock:
                                leads.append(lead)
            except Exception as e:
                push_log(f"  ⚠️  Extra region {country}: {e}")
            time.sleep(random.uniform(1, 2))

    push_log(f"  📦 '{keyword}' → {len(leads)} leads")
    sheet_log_keyword(keyword, len(leads))
    return leads


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SEND — Apps Script rotation
# ══════════════════════════════════════════════════════════════════════════════
def send_email(lead, subject, body):
    if not lead.get("email"): return False
    ok, reason = valid_email(lead["email"])
    if not ok:
        push_log(f"  ⛔ Invalid email ({reason}): {lead['email']}")
        return False

    unsub_base = get_cfg("UNSUBSCRIBE_BASE_URL","")
    unsub_url  = ""
    if unsub_base:
        tok       = _unsub_token(lead["email"])
        unsub_url = f"{unsub_base.rstrip('/')}?email={lead['email']}&token={tok}"

    html_body = build_html_email(body, lead, unsubscribe_url=unsub_url)

    with _email_script_lock:
        n_scripts = len(_email_scripts)
    if n_scripts == 0:
        push_log("  ⛔ No email scripts configured")
        return False

    for _att in range(n_scripts + 1):
        url = _get_active_script()
        if not url:
            push_log("  ⛔ All scripts exhausted")
            return False

        try:
            # Use this script's dedicated IP — Google sees consistent IP per Gmail account
            script_proxy = _get_script_proxy(url)
            r = _post_with_proxy(url, script_proxy, json={
                "to":               lead["email"],
                "subject":          subject,
                "body":             body,
                "html":             html_body,
                "sender_name":      SENDER_NAME,
                "unsubscribe":      unsub_url,
                "list_unsubscribe": unsub_url,
            }, timeout=30)

            if r is None:
                push_log("  ⚠️  Script timeout — rotating")
                _mark_script_failed(url); continue

            try:    result = r.json()
            except: result = {}

            err_text = (result.get("msg","") or r.text or "")[:200].lower()
            is_quota = any(x in err_text for x in ["quota","limit","exceeded","gmail","daily","429"])

            if result.get("status") == "ok" or (r.status_code == 200 and not is_quota and "error" not in err_text[:80]):
                _mark_script_ok(url)
                push_log(f"  📧 Sent → {lead['email']} ({lead['app_name']})")
                return True

            if is_quota:
                push_log("  🔄 Quota hit — rotating script")
                _mark_script_failed(url)
                _mark_script_failed(url)  # double-mark to force rotate
                continue

            push_log(f"  ❌ Script error: {err_text[:80]}")
            _mark_script_failed(url)

        except Exception as e:
            push_log(f"  ❌ Send error: {e}")
            _mark_script_failed(url)

    return False


# ── Main automation ───────────────────────────────────────────────────────────
def run_automation(initial_kw, target, hunter=None):
    global global_seen_ids, global_seen_emails

    upd(running=True, phase="loading_sheet", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[],
        email_script_stats=[])
    stop_event.clear()

    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Start | kw='{initial_kw}' | target={target} | mode={mode}")

    _load_proxy_pool()
    _load_email_scripts()
    load_sheet_memory()

    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY

    all_leads = []
    kws_used  = [initial_kw]
    kw_queue  = [initial_kw]
    empty_streak = 0

    def fallback_kws(base, used):
        suffixes = ["app","mobile","free","pro","tracker","manager","tool","platform","lite","plus"]
        prefixes = ["best","top","new","easy","smart"]
        vs  = [f"{base} {s}" for s in suffixes]
        vs += [f"{p} {base}" for p in prefixes]
        return [v for v in vs if v not in used]

    upd(phase="scraping")

    while len(all_leads) < target and not stop_event.is_set():

        if not kw_queue:
            new_kws = ai_gen_keywords(initial_kw, kws_used)
            if not new_kws:
                new_kws = fallback_kws(initial_kw, kws_used)
            if not new_kws:
                push_log("🔄 No new keywords — re-queuing original after 30s")
                time.sleep(30)
                kw_queue.append(initial_kw)
            else:
                kw_queue.extend(new_kws)

        kw = kw_queue.pop(0)
        if kw not in kws_used: kws_used.append(kw)
        upd(keywords_used=kws_used[:])

        batch = scrape_keyword(kw, hunter, min_leads=MIN_LEADS_PER_KW)
        all_leads.extend(batch)
        upd(leads_found=len(all_leads), leads=[l.copy() for l in all_leads])

        for lead in batch:
            sheet_append_lead(lead)
            sheet_append_qualified(lead)

        if not batch:
            empty_streak += 1
            wait = min(60, 10*empty_streak) + random.uniform(5,10)
            push_log(f"  ⚠️  Empty ({empty_streak} streak) — waiting {wait:.0f}s")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)
        else:
            empty_streak = 0

        push_log(f"📊 {len(all_leads)} / {target} leads")
        if len(all_leads) < target and not stop_event.is_set():
            time.sleep(random.uniform(2, 4))

    if stop_event.is_set():
        upd(running=False, phase="stopped")
        return

    push_log(f"✅ {len(all_leads)} leads collected. Sending emails …")
    upd(phase="emailing")

    for i, lead in enumerate(all_leads):
        if stop_event.is_set(): break
        push_log(f"  ✍️  Writing email {i+1}/{len(all_leads)}: {lead['app_name']}")
        subject, body = ai_gen_email(lead, base_subject, base_body)
        ok = send_email(lead, subject, body)
        lead["email_sent"] = ok
        with state_lock:
            if ok: state["emails_sent"] += 1
            state["leads"] = [l.copy() for l in all_leads]
        if ok: sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])

        if i < len(all_leads)-1 and not stop_event.is_set():
            wait = random.uniform(50, 100)
            push_log(f"  ⏳ {wait:.0f}s delay …")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)

    upd(running=False, phase="stopped" if stop_event.is_set() else "done")
    if not stop_event.is_set():
        push_log("🎉 Done!")


def run_send_pending(leads):
    upd(running=True, phase="emailing")
    stop_event.clear()
    _load_email_scripts()
    push_log(f"📬 Pending: {len(leads)} leads")
    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY
    sent = 0
    for i, lead in enumerate(leads):
        if stop_event.is_set(): break
        subject, body = ai_gen_email(lead, base_subject, base_body)
        ok = send_email(lead, subject, body)
        if ok:
            sent += 1
            sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])
            with state_lock: state["emails_sent"] = state.get("emails_sent",0)+1
        if i < len(leads)-1 and not stop_event.is_set():
            wait = random.uniform(50, 100)
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)
    push_log(f"✅ Sent {sent}/{len(leads)}")
    upd(running=False, phase="done")


def _build_run_cfg(data):
    return {
        "GROQ_API_KEY":         data.get("groq_key")          or os.environ.get("GROQ_API_KEY",""),
        "APPS_SCRIPT_WEB_URL":  data.get("sheet_url")         or os.environ.get("APPS_SCRIPT_WEB_URL",""),
        "EMAIL_SCRIPT_URLS":    data.get("email_script_urls") or os.environ.get("EMAIL_SCRIPT_URLS",""),
        "EMAIL_SUBJECT":        data.get("email_subject")     or os.environ.get("EMAIL_SUBJECT",""),
        "EMAIL_BODY":           data.get("email_body")        or os.environ.get("EMAIL_BODY",""),
        "PROXY_LIST":           data.get("proxy_list")        or os.environ.get("PROXY_LIST",""),
        "SCRAPER_API_KEY":      data.get("scraper_api_key")   or os.environ.get("SCRAPER_API_KEY",""),
        "UNSUBSCRIBE_BASE_URL": data.get("unsubscribe_url")   or os.environ.get("UNSUBSCRIBE_BASE_URL",""),
    }


# ── Routes ────────────────────────────────────────────────────────────────────
@application.route("/")
def index(): return send_from_directory(".","dashboard.html")

@application.route("/api/start", methods=["POST"])
def api_start():
    data    = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword: return jsonify({"error":"keyword required"}), 400
    with state_lock:
        if state["running"]: return jsonify({"error":"Already running"}), 409
    global run_cfg, global_seen_ids, global_seen_emails
    run_cfg            = _build_run_cfg(data)
    global_seen_ids    = set()
    global_seen_emails = set()
    target = int(data.get("target") or os.environ.get("TARGET_LEADS",300))
    hunter = data.get("hunter") or {}
    threading.Thread(target=run_automation, args=(keyword,target,hunter), daemon=True).start()
    return jsonify({"ok":True})

@application.route("/api/stop", methods=["POST"])
def api_stop():
    stop_event.set(); push_log("🛑 Stopped."); return jsonify({"ok":True})

@application.route("/api/status")
def api_status():
    with state_lock: return jsonify(dict(state))

@application.route("/api/clear", methods=["POST"])
def api_clear():
    global global_seen_ids,global_seen_emails,sheet_memory_ids,sheet_memory_emails,sheet_memory_loaded
    with state_lock:
        if state["running"]: return jsonify({"error":"Cannot clear while running"}),409
        state.update({"running":False,"phase":"idle","keyword":"","keywords_used":[],
                      "leads_found":0,"emails_sent":0,"logs":[],"leads":[],"email_script_stats":[]})
    global_seen_ids=set(); global_seen_emails=set()
    sheet_memory_ids=set(); sheet_memory_emails=set(); sheet_memory_loaded=False
    return jsonify({"ok":True})

@application.route("/api/ping", methods=["GET","POST"])
def api_ping(): return jsonify({"ok":True,"ts":time.time()})

@application.route("/api/send_pending", methods=["POST"])
def api_send_pending():
    with state_lock:
        if state["running"]: return jsonify({"error":"Running"}),409
    data  = request.get_json(silent=True) or {}
    leads = data.get("leads") or []
    if not leads: return jsonify({"error":"No leads"}),400
    global run_cfg
    run_cfg = _build_run_cfg(data)
    threading.Thread(target=run_send_pending, args=(leads,), daemon=True).start()
    return jsonify({"ok":True,"count":len(leads)})

@application.route("/api/spam_test", methods=["POST"])
def api_spam_test():
    data    = request.get_json(silent=True) or {}
    test_to = (data.get("test_email") or "").strip()
    if not test_to: return jsonify({"error":"test_email required"}),400
    global run_cfg
    run_cfg = _build_run_cfg(data)
    _load_email_scripts()
    sample = {
        "app_name":"MyFinance Pro","developer":"John Dev","category":"Finance",
        "installs":2500,"score":2.8,"email":test_to,
        "url":"https://play.google.com/store/apps/details?id=com.example","app_id":"com.example",
    }
    subj, body = ai_gen_email(sample, get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT,
                              get_cfg("EMAIL_BODY") or DEFAULT_EMAIL_BODY)
    ok = send_email(sample, subj, body)
    if ok: return jsonify({"ok":True,"msg":f"Sent to {test_to}","subject":subj,"body":body})
    return jsonify({"error":"Send failed — check email script URLs"}),500

@application.route("/api/sheet_pending", methods=["POST"])
def api_sheet_pending():
    data      = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL","")
    if not sheet_url: return jsonify({"error":"sheet_url not set"}),400
    try:
        r      = robust_post(sheet_url, json={"action":"get_pending"}, timeout=20)
        result = r.json() if (r and r.text) else {}
        leads  = result.get("leads",[])
        return jsonify({"ok":True,"count":len(leads),"leads":leads})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@application.route("/api/proxy_status", methods=["GET","POST"])
def api_proxy_status():
    """GET = just status. POST = load proxies from body then return status."""
    data = {}
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        # Accept proxy config from dashboard and load immediately
        if data.get("proxy_list") or data.get("scraper_api_key"):
            global run_cfg
            run_cfg["PROXY_LIST"]      = data.get("proxy_list","")
            run_cfg["SCRAPER_API_KEY"] = data.get("scraper_api_key","")
            _load_proxy_pool()

    sa = get_cfg("SCRAPER_API_KEY","")
    with _proxy_lock:
        total   = len(_proxy_pool)
        healthy = sum(1 for p in _proxy_pool if _proxy_fail_counts.get(p,0)<MAX_PROXY_FAILS)
    return jsonify({
        "scraper_api_mode": bool(sa),
        "proxy_pool_total": total,
        "healthy":          healthy,
        "retired":          total - healthy,
    })

@application.route("/api/email_script_status", methods=["GET"])
def api_email_script_status():
    with _email_script_lock:
        cur   = _current_script_idx % max(len(_email_scripts),1)
        stats = [{"index":i+1,"url":u[:70]+"…" if len(u)>70 else u,
                  "sent":_script_sent_counts.get(u,0),"fails":_script_fail_counts.get(u,0),
                  "active":i==cur,
                  "status":"quota" if _script_sent_counts.get(u,0)>=MAX_SENDS_PER_SCRIPT
                            else "retired" if _script_fail_counts.get(u,0)>=MAX_SCRIPT_FAILS
                            else "active"}
                 for i,u in enumerate(_email_scripts)]
    return jsonify({"scripts":stats,"total":len(_email_scripts),"max_per_script":MAX_SENDS_PER_SCRIPT})

@application.route("/api/sheet_memory_status", methods=["GET"])
def api_sheet_memory_status():
    with sheet_memory_lock:
        return jsonify({"loaded":sheet_memory_loaded,"ids":len(sheet_memory_ids),"emails":len(sheet_memory_emails)})

@application.route("/unsubscribe", methods=["GET"])
def unsubscribe():
    email = request.args.get("email","").strip().lower()
    token = request.args.get("token","").strip()
    if not email or token != _unsub_token(email):
        return "<h2>Invalid link.</h2>",400
    push_log(f"📭 Unsubscribe: {email}")
    sheet_post({"action":"append","tab":"Unsubscribes","row":{
        "Email":email,"At":time.strftime("%Y-%m-%d %H:%M:%S")}})
    global_seen_emails.add(email)
    register("",email)
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Unsubscribed</title>
<style>body{{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;background:#f9f9f9;}}
.box{{background:#fff;padding:40px;border-radius:8px;text-align:center;max-width:400px;
box-shadow:0 1px 4px rgba(0,0,0,.1);}}h2{{color:#222;}}p{{color:#555;}}</style></head>
<body><div class="box"><h2>Unsubscribed</h2>
<p>{email} removed. No more emails from PlayReview.</p></div></body></html>"""

@application.route("/api/reassign_proxies", methods=["POST"])
def api_reassign_proxies():
    """Randomly reassign proxy IPs to script URLs (Change Direction)."""
    with _email_script_lock:
        urls = list(_email_scripts)
    if not urls:
        return jsonify({"error": "No scripts loaded yet"}), 400
    _assign_proxies_to_scripts(urls)
    with _script_proxy_lock:
        pairs = [{"script": i+1, "proxy": (v or {}).get("http","direct")[:40]+"…"
                  if v and len((v or {}).get("http","")) > 40
                  else (v or {}).get("http","direct") if v else "direct"}
                 for i, (k, v) in enumerate(_script_proxy_map.items())]
    push_log(f"🔀 Proxy directions reassigned for {len(urls)} scripts")
    return jsonify({"ok": True, "pairs": pairs})


if __name__ == "__main__":
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
