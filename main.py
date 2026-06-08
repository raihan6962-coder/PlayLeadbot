"""
PlayLead Engine -- v5
====================
Key fix: Auto-fetch fresh working proxies from GitHub -> test them ->
use working ones to monkey-patch google-play-scraper's urllib calls.
Railway datacenter IPs are blocked by Play Store, so we need real proxies.
"""

import os, time, random, threading, json, re, logging, socket, hashlib
import urllib.request, urllib.error, ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from google_play_scraper import search, app as gp_app
from groq import Groq
import requests as req_lib

def parse_installs(val) -> int:
    """Convert any install field format to int.
    Handles: int, "1,000,000+", "1M+", "500K+", None, "Varies"
    """
    if val is None: return 0
    if isinstance(val, (int, float)): return int(val)
    s = str(val).strip().upper().replace(",", "").replace("+", "").replace(" ", "")
    if not s or s in ("VARIESWITHDEVICE", "VARIES", ""): return 0
    try:
        if s.endswith("B"):   return int(float(s[:-1]) * 1_000_000_000)
        if s.endswith("M"):   return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"):   return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except: return 0


application = Flask(__name__, static_folder=".")
app = application
CORS(application)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

stop_event  = threading.Event()
state_lock  = threading.Lock()
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
sheet_memory_lock         = threading.Lock()
run_cfg = {}

def get_cfg(key, fallback=""):
    return run_cfg.get(key) or os.environ.get(key, fallback)

def push_log(msg):
    with state_lock:
        state["logs"].append({"time": time.strftime("%H:%M:%S"), "msg": msg})
        if len(state["logs"]) > 600:
            state["logs"] = state["logs"][-600:]
    log.info(msg)

def upd(**kw):
    with state_lock:
        state.update(kw)

# ── SSL context ───────────────────────────────────────────────────────────────
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode    = ssl.CERT_NONE

PLAY_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ══════════════════════════════════════════════════════════════════════════════
# PROXY MANAGER -- auto-fetch + test + rotate
# ══════════════════════════════════════════════════════════════════════════════
_proxy_lock     = threading.Lock()
_working_proxies: list = []    # tested working proxies: "host:port" strings
_proxy_idx: int = 0
_last_proxy_refresh: float = 0
PROXY_TTL = 1800   # re-test every 30 min

# GitHub-hosted proxy lists (Railway can reach GitHub)
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]


def _fetch_proxy_list_from_github() -> list:
    # Fetch fresh proxy list from GitHub (Railway allows raw.githubusercontent.com).
    all_proxies = []
    for url in PROXY_SOURCES:
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                context=SSL_CTX, timeout=10
            )
            lines = r.read().decode().strip().split("\n")
            proxies = [l.strip() for l in lines if re.match(r'^\d+\.\d+\.\d+\.\d+:\d+$', l.strip())]
            all_proxies.extend(proxies)
            push_log(f"  📥 {len(proxies)} proxies from GitHub source")
            if len(all_proxies) > 500:
                break
        except Exception as e:
            push_log(f"  ⚠️  Proxy source fail: {str(e)[:50]}")
    random.shuffle(all_proxies)
    return list(dict.fromkeys(all_proxies))  # dedupe


def _test_proxy(proxy_addr: str, timeout: int = 6) -> bool:
    # Test if a proxy can reach Play Store.
    try:
        handler  = urllib.request.ProxyHandler({"http": f"http://{proxy_addr}", "https": f"http://{proxy_addr}"})
        opener   = urllib.request.build_opener(handler)
        req_obj  = urllib.request.Request(
            "https://play.google.com/store/apps/details?id=com.google.android.gm&hl=en&gl=us",
            headers=PLAY_HEADERS
        )
        resp = opener.open(req_obj, timeout=timeout)
        content = resp.read(500).decode("utf-8", errors="ignore")
        return "com.google.android.gm" in content or "Gmail" in content or len(content) > 100
    except Exception:
        return False


def _test_proxy_fast(proxy_addr: str, timeout: int = 5) -> bool:
    # Faster test -- just check TCP + HTTP response code.
    try:
        handler = urllib.request.ProxyHandler({"http": f"http://{proxy_addr}", "https": f"http://{proxy_addr}"})
        opener  = urllib.request.build_opener(handler)
        req_obj = urllib.request.Request("http://play.google.com/", headers={"User-Agent": "Mozilla/5.0"})
        resp = opener.open(req_obj, timeout=timeout)
        return resp.status < 500
    except urllib.error.HTTPError as e:
        # 403 from Play Store means the proxy itself works!
        return e.code in (403, 200, 301, 302)
    except Exception:
        return False


def refresh_proxy_pool(force: bool = False):
    # Fetch + test proxies, keep the working ones.
    global _working_proxies, _proxy_idx, _last_proxy_refresh

    now = time.time()
    if not force and (now - _last_proxy_refresh) < PROXY_TTL and _working_proxies:
        return

    # Also include user-configured proxies
    user_proxies = []
    raw = get_cfg("PROXY_LIST", "")
    for line in raw.split("\n"):
        line = line.strip()
        if not line: continue
        # Strip protocol prefix if present
        line = re.sub(r'^https?://', '', line)
        if re.match(r'.+:.+@.+:\d+', line):
            user_proxies.append(line)   # user:pass@host:port -- keep as-is
        elif re.match(r'^\d+\.\d+\.\d+\.\d+:\d+$', line):
            user_proxies.append(line)

    push_log(f"🔄 Fetching fresh proxy list …")
    github_proxies = _fetch_proxy_list_from_github()

    all_candidates = user_proxies + github_proxies
    push_log(f"  Testing {min(len(all_candidates), 150)} proxies (parallel) …")

    working = list(user_proxies)  # trust user proxies without testing
    to_test = [p for p in github_proxies if p not in user_proxies][:150]

    with ThreadPoolExecutor(max_workers=40) as ex:
        fut_map = {ex.submit(_test_proxy_fast, p): p for p in to_test}
        for fut in as_completed(fut_map):
            if fut.result():
                working.append(fut_map[fut])

    random.shuffle(working)
    with _proxy_lock:
        _working_proxies = working
        _proxy_idx       = 0
        _last_proxy_refresh = time.time()

    push_log(f"✅ {len(working)} working proxies ready")


def get_proxy_opener():
    # Return a urllib opener with the next working proxy.
    global _proxy_idx

    # ScraperAPI mode
    sa_key = get_cfg("SCRAPER_API_KEY", "")
    if sa_key:
        proxy_url = f"http://scraperapi:{sa_key}@proxy-server.scraperapi.com:8001"
        handler   = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        return urllib.request.build_opener(handler)

    with _proxy_lock:
        if not _working_proxies:
            return urllib.request.build_opener()   # direct (may fail)
        proxy_addr = _working_proxies[_proxy_idx % len(_working_proxies)]
        _proxy_idx += 1

    # Handle user:pass@host:port format
    if "@" in proxy_addr:
        proxy_url = f"http://{proxy_addr}"
    else:
        proxy_url = f"http://{proxy_addr}"

    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    return urllib.request.build_opener(handler)


def _urlopen_with_proxy(url_or_req, timeout=20):
    # Open a URL via rotating proxy using urllib.
    opener = get_proxy_opener()
    if isinstance(url_or_req, str):
        url_or_req = urllib.request.Request(url_or_req, headers=PLAY_HEADERS)
    return opener.open(url_or_req, timeout=timeout)


# ── Monkey-patch google-play-scraper to use our proxy opener ─────────────────
import google_play_scraper.utils.request as _gps_req

_orig_urlopen = urllib.request.urlopen

def _patched_urlopen(url_or_req, *args, **kwargs):
    # Intercept all urlopen calls from google-play-scraper and route via proxy.
    kwargs.pop("context", None)   # remove conflicting context
    opener = get_proxy_opener()
    if isinstance(url_or_req, str):
        url_or_req = urllib.request.Request(url_or_req, headers=PLAY_HEADERS)
    elif isinstance(url_or_req, urllib.request.Request):
        for k, v in PLAY_HEADERS.items():
            if not url_or_req.get_header(k.capitalize()):
                url_or_req.add_header(k, v)
    timeout = kwargs.get("timeout", 20)
    try:
        return opener.open(url_or_req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise
    except Exception:
        # fallback: try direct
        return _orig_urlopen(url_or_req, context=SSL_CTX, timeout=timeout)

# Apply the patch
urllib.request.urlopen = _patched_urlopen


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SCRIPT ROTATION
# ══════════════════════════════════════════════════════════════════════════════
_email_scripts:      list = []
_email_script_lock        = threading.Lock()
_script_fail_counts: dict = {}
_script_sent_counts: dict = {}
_current_script_idx: int  = 0
_script_proxy_map:   dict = {}
_script_proxy_lock        = threading.Lock()
MAX_SCRIPT_FAILS           = 3
MAX_SENDS_PER_SCRIPT       = 80

def _assign_proxies_to_scripts(urls):
    with _proxy_lock:
        pool = list(_working_proxies)
    with _script_proxy_lock:
        _script_proxy_map.clear()
        if not pool:
            for u in urls:
                _script_proxy_map[u] = None
            return
        random.shuffle(pool)
        for i, u in enumerate(urls):
            _script_proxy_map[u] = pool[i % len(pool)]
    push_log(f"🔗 IPs assigned: {len(urls)} scripts -> {min(len(urls),len(pool))} unique IPs")

def _get_script_proxy(script_url):
    with _script_proxy_lock:
        return _script_proxy_map.get(script_url)

def _load_email_scripts():
    global _email_scripts, _current_script_idx, _script_fail_counts, _script_sent_counts
    raw  = get_cfg("EMAIL_SCRIPT_URLS","") or get_cfg("EMAIL_SCRIPT_URL","")
    urls = [u.strip() for u in raw.split("\n") if u.strip().startswith("http")]
    with _email_script_lock:
        _email_scripts      = urls
        _current_script_idx = 0
        _script_fail_counts = {u:0 for u in urls}
        _script_sent_counts = {u:0 for u in urls}
    _assign_proxies_to_scripts(urls)
    _refresh_script_stats()
    if urls: push_log(f"📧 {len(urls)} email scripts loaded")
    return urls

def _get_active_script():
    with _email_script_lock:
        if not _email_scripts: return ""
        start = _current_script_idx % len(_email_scripts)
        for offset in range(len(_email_scripts)):
            idx = (start+offset) % len(_email_scripts)
            u   = _email_scripts[idx]
            if _script_fail_counts.get(u,0)<MAX_SCRIPT_FAILS and _script_sent_counts.get(u,0)<MAX_SENDS_PER_SCRIPT:
                return u
        for u in _email_scripts:
            _script_fail_counts[u]=0; _script_sent_counts[u]=0
        return _email_scripts[0]

def _mark_script_ok(url):
    global _current_script_idx
    with _email_script_lock:
        _script_fail_counts[url]=0
        _script_sent_counts[url]=_script_sent_counts.get(url,0)+1
        if _script_sent_counts[url]>=MAX_SENDS_PER_SCRIPT and url in _email_scripts:
            idx=_email_scripts.index(url)
            _current_script_idx=(idx+1)%len(_email_scripts)
            push_log(f"  🔄 Script #{idx+1} -> {_current_script_idx+1}")
    _refresh_script_stats()

def _mark_script_failed(url):
    global _current_script_idx
    with _email_script_lock:
        _script_fail_counts[url]=_script_fail_counts.get(url,0)+1
        if _script_fail_counts[url]>=MAX_SCRIPT_FAILS and url in _email_scripts:
            idx=_email_scripts.index(url)
            _current_script_idx=(idx+1)%len(_email_scripts)
    _refresh_script_stats()

def _refresh_script_stats():
    with _email_script_lock:
        cur=_current_script_idx%max(len(_email_scripts),1)
        stats=[{"index":i+1,"url":u[:65]+"…" if len(u)>65 else u,
                "sent":_script_sent_counts.get(u,0),"fails":_script_fail_counts.get(u,0),
                "active":i==cur,
                "status":"quota" if _script_sent_counts.get(u,0)>=MAX_SENDS_PER_SCRIPT
                          else "retired" if _script_fail_counts.get(u,0)>=MAX_SCRIPT_FAILS
                          else "active"}
               for i,u in enumerate(_email_scripts)]
    with state_lock:
        state["email_script_stats"]=stats


# ── Country filter ────────────────────────────────────────────────────────────
BLOCKED_COUNTRIES = {
    "BD","IN","PK","NG","GH","KE","TZ","UG","ET","EG","MA","TN","DZ","LY",
    "SD","SO","AO","MZ","ZM","ZW","MW","RW","SN","CI","CM","CD","MG","MM",
    "KH","LA","NP","LK","AF","IQ","SY","YE","LB","JO","PS","PH","ID","VN","TH","MY",
}
BLOCKED_ADDR_KW = [
    "bangladesh","dhaka","india","mumbai","delhi","bangalore","pakistan","karachi",
    "nigeria","lagos","kenya","nairobi","ghana","indonesia","jakarta",
    "philippines","vietnam","myanmar","nepal","sri lanka","ethiopia",
    "egypt","morocco","tanzania","uganda",
]
def is_allowed_country(d):
    cc=(d.get("developerCountry") or d.get("country") or "").upper()
    if cc and cc in BLOCKED_COUNTRIES: return False
    addr=(d.get("developerAddress") or "").lower()
    return not any(k in addr for k in BLOCKED_ADDR_KW)


# ── Google Sheet helpers ──────────────────────────────────────────────────────
def sheet_post(payload):
    url=get_cfg("APPS_SCRIPT_WEB_URL")
    if not url: return None
    try:
        r=req_lib.post(url,json=payload,timeout=15)
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

def sheet_mark_sent(app_id,email,app_name):
    sheet_post({"action":"mark_sent","app_id":app_id})
    sheet_post({"action":"append","tab":"Email Sent","row":{
        "App ID":app_id,"App Name":app_name,
        "Email":email,"Sent At":time.strftime("%Y-%m-%d %H:%M:%S"),
    }})

def sheet_log_keyword(keyword,count):
    sheet_post({"action":"append","tab":"Keyword Log","row":{
        "Keyword":keyword,"Leads Found":count,
        "Logged At":time.strftime("%Y-%m-%d %H:%M:%S"),
    }})

def load_sheet_memory():
    global sheet_memory_ids,sheet_memory_emails,sheet_memory_loaded
    url=get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        push_log("⚠️  No sheet URL")
        with sheet_memory_lock: sheet_memory_loaded=True
        return
    push_log("📋 Loading sheet memory …")
    try:
        r=req_lib.post(url,json={"action":"get_all","tab":"All Leads"},timeout=30)
        result=r.json() if (r and r.text) else {}
        records=result.get("records",[])
        ids,ems=set(),set()
        for rec in records:
            if rec.get("App ID"): ids.add(rec["App ID"].strip())
            if rec.get("Email"):  ems.add(rec["Email"].strip().lower())
        with sheet_memory_lock:
            sheet_memory_ids=ids; sheet_memory_emails=ems; sheet_memory_loaded=True
        push_log(f"✅ Sheet: {len(ids)} apps, {len(ems)} emails")
    except Exception as e:
        push_log(f"⚠️  Sheet load fail: {e}")
        with sheet_memory_lock: sheet_memory_loaded=True

def is_dup(app_id,email):
    with sheet_memory_lock:
        if app_id and app_id in sheet_memory_ids:          return True
        if email   and email.lower() in sheet_memory_emails: return True
    return False

def register(app_id,email):
    with sheet_memory_lock:
        if app_id: sheet_memory_ids.add(app_id)
        if email:  sheet_memory_emails.add(email.lower())


# ── Email validation ──────────────────────────────────────────────────────────
DISPOSABLE={"mailinator.com","guerrillamail.com","10minutemail.com","trashmail.com",
            "yopmail.com","throwam.com","sharklasers.com","spam4.me","tempmail.com",
            "fakeinbox.com","maildrop.cc","dispostable.com","mailnull.com"}
EMAIL_RE  =re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
SYNTAX_RE =re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def valid_email(email):
    if not email: return False,"empty"
    email=email.strip().lower()
    if not SYNTAX_RE.match(email): return False,"bad_syntax"
    domain=email.split("@")[-1]
    if domain in DISPOSABLE: return False,"disposable"
    try:
        socket.setdefaulttimeout(4)
        socket.getaddrinfo(domain,None)
    except: return False,"no_dns"
    return True,"ok"

def extract_email(text):
    if not text: return ""
    m=EMAIL_RE.search(str(text))
    return m.group(0) if m else ""


# ── FILTER ────────────────────────────────────────────────────────────────────
def passes_filter(installs, score, hunter):
    """
    HUNTER MODE: installs <= max_installs AND score <= max_score (must have rating)
    NORMAL MODE: installs <= 10,000 AND no rating at all (brand new apps only)
    """
    installs = parse_installs(installs)
    if score is not None:
        try: score = float(score)
        except: score = None
    if score == 0.0: score = None

    if hunter and hunter.get("active"):
        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score") or 2.5)
        if installs > max_inst:         return False
        if score is None or score == 0: return False
        if score > max_score:           return False
        return True

    if installs > 10_000:               return False
    if score is not None and score > 0: return False
    return True


# ── Keyword generation ────────────────────────────────────────────────────────
KW_SYSTEM="""You are a Google Play Store keyword expert for finding apps needing review management.

CONTEXT: Target apps with poor ratings (Hunter Mode) or brand new with no ratings (Normal Mode).

STRICT RULES:
- Stay in the EXACT same niche as the original keyword
- BAD: "crypto wallet" -> "cryptocurrency calculator"
- GOOD: "crypto wallet" -> "bitcoin wallet mobile", "ethereum wallet app"
- Keywords must be 2-5 words, real Play Store search queries
- Niches: fintech, productivity, business, health, education, food delivery, e-commerce

Return ONLY a valid JSON array. No markdown, no explanation."""

def ai_gen_keywords(original,used):
    key=get_cfg("GROQ_API_KEY")
    if not key: return []
    try:
        client=Groq(api_key=key)
        prompt=(
            f"Original keyword: '{original}'\n"
            f"Already used (do NOT repeat): {', '.join(used[-20:]) if used else 'none'}\n\n"
            f"Generate exactly 8 NEW Google Play Store search keywords in the SAME niche as '{original}'.\n"
            f"Keep intent tightly aligned -- same niche. Return ONLY a JSON array of 8 strings."
        )
        resp=client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":KW_SYSTEM},{"role":"user","content":prompt}],
            temperature=0.7,max_tokens=400
        )
        raw=resp.choices[0].message.content.strip()
        raw=re.sub(r"```[a-z]*","",raw).replace("```","").strip()
        kws=json.loads(raw)
        valid=[k for k in kws if isinstance(k,str) and k.strip() and k not in used]
        push_log(f"🤖 AI keywords: {valid[:6]}")
        return valid
    except Exception as e:
        push_log(f"AI kw error: {e}"); return []


# ── Email template ────────────────────────────────────────────────────────────
SENDER_NAME="PlayReview"
DEFAULT_EMAIL_SUBJECT="Your {{app_name}} reviews on Google Play"
DEFAULT_EMAIL_BODY = (
    "Hi {{developer}},\n\n"
    "I noticed {{app_name}} on Google Play{{score_line}}.\n\n"
    "Apps in your category live or die by their Play Store rating -- "
    "even a 0.5 star improvement can double install rates.\n\n"
    "We help app developers clean up their Play Store presence: removing unfair reviews, "
    "building genuine social proof, and protecting their rating long-term.\n\n"
    "If you are open to a quick 10-minute call this week, "
    "I would love to show you what we have done for similar apps.\n\n"
    "Best,\nPlayReview"
)

def build_html_email(plain_body,lead,unsubscribe_url=""):
    lines=plain_body.strip().split("\n")
    html_lines=[]
    for line in lines:
        line=line.strip()
        if line:
            html_lines.append(f'<p style="margin:0 0 12px;font-size:14px;line-height:1.6;color:#222;">{line}</p>')
        else:
            html_lines.append('<br>')
    body_html="\n".join(html_lines)
    unsub=""
    if unsubscribe_url:
        unsub=(f'<p style="margin:32px 0 0;font-size:11px;color:#aaa;border-top:1px solid #eee;'
               f'padding-top:12px;text-align:center;">'
               f'<a href="{unsubscribe_url}" style="color:#aaa;text-decoration:underline;">Unsubscribe</a></p>')
    return (f'<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
            f'<body style="margin:0;padding:20px;font-family:Arial,sans-serif;background:#fff;max-width:580px;">'
            f'{body_html}{unsub}</body></html>')

def fill_template(tpl,lead):
    score=lead.get("score")
    score_line=f" -- currently rated {score:.1f}★" if score and score>0 else " (just launched, building reviews)"
    return (tpl.replace("{{app_name}}",lead.get("app_name",""))
               .replace("{{developer}}",lead.get("developer",""))
               .replace("{{category}}",lead.get("category",""))
               .replace("{{installs}}",str(lead.get("installs","")))
               .replace("{{score}}",str(score or "N/A"))
               .replace("{{score_line}}",score_line)
               .replace("{{url}}",lead.get("url",""))
               .replace("{{sender_name}}",SENDER_NAME))

def ai_gen_email(lead,base_subject,base_body):
    key=get_cfg("GROQ_API_KEY")
    if not key:
        return fill_template(base_subject,lead),fill_template(base_body,lead)
    score=lead.get("score")
    score_info=f"{score:.1f} stars" if score else "no rating yet (brand new)"
    install_info=f"{lead['installs']:,} installs" if lead.get("installs") else "just launched"
    try:
        client=Groq(api_key=key)
        prompt=(f"Personalise this cold email for a specific app developer.\n"
                f"Short, direct, personal. Max 120 words.\n\n"
                f"TEMPLATE:\nSubject: {base_subject}\nBody:\n{base_body}\n\n"
                f"APP: {lead.get('app_name','')} | Dev: {lead.get('developer','')} | "
                f"Cat: {lead.get('category','')} | {install_info} | {score_info}\n\n"
                f"Return ONLY JSON: {{\"subject\":\"...\",\"body\":\"...\"}}\nUse \\n for line breaks.")
        resp=client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"user","content":prompt}],
            temperature=0.4,max_tokens=400
        )
        raw=resp.choices[0].message.content.strip()
        raw=re.sub(r"```[a-z]*","",raw).replace("```","").strip()
        data=json.loads(raw)
        subj=data.get("subject") or fill_template(base_subject,lead)
        body=data.get("body")    or fill_template(base_body,lead)
        return subj,body.replace("\\n","\n")
    except Exception as e:
        push_log(f"  AI email fallback: {e}")
        return fill_template(base_subject,lead),fill_template(base_body,lead)

def _unsub_token(email):
    salt=os.environ.get("UNSUB_SALT","playleadbot-2024")
    return hashlib.sha256(f"{salt}:{email.lower()}".encode()).hexdigest()[:32]


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPER -- parallel, proxy-patched, fast
# ══════════════════════════════════════════════════════════════════════════════
SEARCH_COMBOS = [
    ("en", "us"), ("en", "gb"), ("en", "au"), ("en", "ca"),
]
MIN_LEADS_PER_KW = 2


def _play_search(keyword,lang,country,n_hits=250):
    # Search Play Store -- proxy is injected via monkey-patched urlopen.
    for attempt in range(3):
        try:
            results=search(keyword,lang=lang,country=country,n_hits=n_hits)
            return results
        except Exception as e:
            err=str(e).lower()
            if any(x in err for x in ["429","403","rate","blocked","captcha","gateway"]):
                wait=15*(attempt+1)+random.uniform(3,8)
                push_log(f"  🚦 Rate-limit ({country}) -- {wait:.0f}s")
                time.sleep(wait)
            elif attempt==2:
                raise
            else:
                time.sleep(random.uniform(2,4))
    return []


def fetch_app_details_reliable(app_id: str):
    # Multi-region detail fetch -- proxy injected via monkey-patched urlopen
    first_result = None
    for lang, country in [("en","us"), ("en","gb"), ("en","au")]:
        try:
            details = gp_app(app_id, lang=lang, country=country)
            if first_result is None:
                first_result = details
            if details.get("score") is not None and details.get("score", 0) > 0:
                return details
        except Exception:
            time.sleep(random.uniform(0.5, 1.5))
            continue
    return first_result


def scrape_keyword(keyword: str, hunter: dict = None, min_leads: int = MIN_LEADS_PER_KW) -> list:
    # Original proven scrape logic -- multi-region, full detail fetch, strict filter
    global global_seen_ids, global_seen_emails
    mode_label = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"Scraping [{mode_label}]: {keyword}")
    leads = []

    for lang, country in SEARCH_COMBOS:
        if stop_event.is_set():
            break

        # Search with retry
        results = []
        for attempt in range(3):
            try:
                results = search(keyword, lang=lang, country=country, n_hits=500)
                push_log(f"  [{country}] {len(results)} results")
                break
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ["429", "403", "rate", "blocked", "captcha", "gateway"]):
                    wait = 15 * (attempt + 1) + random.uniform(3, 8)
                    push_log(f"  Rate-limit ({country}) -- {wait:.0f}s wait")
                    time.sleep(wait)
                elif attempt == 2:
                    push_log(f"  Search error ({country}): {str(e)[:60]}")
                else:
                    time.sleep(random.uniform(2, 4))

        for item in results:
            if stop_event.is_set():
                break

            app_id = item.get("appId", "")
            if not app_id or app_id in global_seen_ids:
                continue
            if is_dup(app_id, ""):
                global_seen_ids.add(app_id)
                continue

            # Full detail fetch -- multi-region for reliable rating data
            details = fetch_app_details_reliable(app_id)
            if details is None:
                global_seen_ids.add(app_id)
                continue

            installs = parse_installs(details.get("minInstalls") or details.get("installs") or 0)
            score    = details.get("score")
            if score is not None:
                try:    score = float(score)
                except: score = None
            if score == 0.0:
                score = None

            if not passes_filter(installs, score, hunter):
                global_seen_ids.add(app_id)
                continue

            if not is_allowed_country(details):
                global_seen_ids.add(app_id)
                push_log(f"  Blocked country: {details.get('title', app_id)}")
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

            email = email.lower().strip()
            ok, reason = valid_email(email)
            if not ok:
                global_seen_ids.add(app_id)
                push_log(f"  Bad email ({reason}): {email}")
                continue

            if email in global_seen_emails or is_dup("", email):
                global_seen_ids.add(app_id)
                push_log(f"  Dup email: {email}")
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
            register(app_id, email)

            s_str = f"{score:.1f}star" if score else "new"
            push_log(f"  OK [{mode_label}] {lead['app_name']} | {installs:,} | {s_str} | {email}")
            time.sleep(0.3)

        push_log(f"  [{country}] done. Leads so far: {len(leads)}")
        time.sleep(0.5)

    push_log(f"  {len(leads)} new leads from: {keyword}")
    sheet_log_keyword(keyword, len(leads))
    return leads

def send_email(lead,subject,body):
    if not lead.get("email"): return False
    ok,reason=valid_email(lead["email"])
    if not ok: push_log(f"  ⛔ Bad email ({reason}): {lead['email']}"); return False

    unsub_base=get_cfg("UNSUBSCRIBE_BASE_URL","")
    unsub_url=""
    if unsub_base:
        tok=_unsub_token(lead["email"])
        unsub_url=f"{unsub_base.rstrip('/')}?email={lead['email']}&token={tok}"

    html_body=build_html_email(body,lead,unsubscribe_url=unsub_url)

    with _email_script_lock:
        n_scripts=len(_email_scripts)
    if n_scripts==0:
        push_log("  ⛔ No email scripts configured"); return False

    for _ in range(n_scripts+1):
        url=_get_active_script()
        if not url: push_log("  ⛔ Scripts exhausted"); return False

        proxy_addr=_get_script_proxy(url)
        proxies=None
        if proxy_addr:
            if "@" in str(proxy_addr):
                pu=f"http://{proxy_addr}"
            else:
                pu=f"http://{proxy_addr}"
            proxies={"http":pu,"https":pu}

        try:
            r=req_lib.post(url,json={
                "to":lead["email"],"subject":subject,"body":body,"html":html_body,
                "sender_name":SENDER_NAME,"unsubscribe":unsub_url,"list_unsubscribe":unsub_url,
            },proxies=proxies,timeout=30)
            try:    result=r.json()
            except: result={}
            err_text=(result.get("msg","") or r.text or "")[:200].lower()
            is_quota=any(x in err_text for x in ["quota","limit","exceeded","gmail","daily","429"])
            if result.get("status")=="ok" or (r.status_code==200 and not is_quota and "error" not in err_text[:60]):
                _mark_script_ok(url)
                push_log(f"  📧 Sent -> {lead['email']}")
                return True
            if is_quota:
                push_log("  🔄 Quota hit -- next script")
                _mark_script_failed(url); _mark_script_failed(url); continue
            push_log(f"  ❌ Script err: {err_text[:60]}")
            _mark_script_failed(url)
        except Exception as e:
            push_log(f"  ❌ Send err: {e}")
            _mark_script_failed(url)
    return False


# ── Main automation ───────────────────────────────────────────────────────────
def run_automation(initial_kw,target,hunter=None):
    global global_seen_ids,global_seen_emails
    upd(running=True,phase="loading_sheet",keyword=initial_kw,
        keywords_used=[],leads_found=0,emails_sent=0,logs=[],leads=[],email_script_stats=[])
    stop_event.clear()
    mode="Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Start | kw='{initial_kw}' | target={target} | mode={mode}")

    push_log("🔄 Loading proxies …")
    refresh_proxy_pool(force=True)
    _load_email_scripts()
    load_sheet_memory()

    base_subject=get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body   =get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY

    all_leads=[];kws_used=[initial_kw];kw_queue=[initial_kw];empty_streak=0

    def fallback_kws(base,used):
        mods=["app","mobile","free","pro","lite","plus","tracker","service","platform"]
        vs=[f"{base} {m}" for m in mods]+[f"best {base}",f"top {base}",f"new {base}"]
        return [v for v in vs if v not in used]

    upd(phase="scraping")
    while len(all_leads)<target and not stop_event.is_set():
        if not kw_queue:
            new_kws=ai_gen_keywords(initial_kw,kws_used)
            if not new_kws: new_kws=fallback_kws(initial_kw,kws_used)
            if not new_kws:
                push_log("🔄 Re-queuing original after 30s")
                time.sleep(30); kw_queue.append(initial_kw)
            else:
                kw_queue.extend(new_kws)

        kw=kw_queue.pop(0)
        if kw not in kws_used: kws_used.append(kw)
        upd(keywords_used=kws_used[:])

        batch=scrape_keyword(kw,hunter,min_leads=MIN_LEADS_PER_KW)
        all_leads.extend(batch)
        upd(leads_found=len(all_leads),leads=[l.copy() for l in all_leads])

        for lead in batch:
            sheet_append_lead(lead); sheet_append_qualified(lead)

        if not batch:
            empty_streak+=1
            wait=min(45,8*empty_streak)+random.uniform(3,8)
            push_log(f"  ⚠️  Empty ({empty_streak}) -- {wait:.0f}s wait + refreshing proxies")
            refresh_proxy_pool()  # refresh proxies on empty streak
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)
        else:
            empty_streak=0

        push_log(f"📊 {len(all_leads)}/{target}")
        if len(all_leads)<target and not stop_event.is_set():
            time.sleep(random.uniform(1,3))

    if stop_event.is_set():
        upd(running=False,phase="stopped"); return

    push_log(f"✅ {len(all_leads)} leads. Sending emails …")
    upd(phase="emailing")
    for i,lead in enumerate(all_leads):
        if stop_event.is_set(): break
        push_log(f"  ✍️  {i+1}/{len(all_leads)}: {lead['app_name']}")
        subj,body=ai_gen_email(lead,base_subject,base_body)
        ok=send_email(lead,subj,body)
        lead["email_sent"]=ok
        with state_lock:
            if ok: state["emails_sent"]+=1
            state["leads"]=[l.copy() for l in all_leads]
        if ok: sheet_mark_sent(lead["app_id"],lead["email"],lead["app_name"])
        if i<len(all_leads)-1 and not stop_event.is_set():
            wait=random.uniform(50,100)
            push_log(f"  ⏳ {wait:.0f}s …")
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)

    upd(running=False,phase="stopped" if stop_event.is_set() else "done")
    if not stop_event.is_set(): push_log("🎉 Done!")

def run_send_pending(leads):
    upd(running=True,phase="emailing"); stop_event.clear()
    _load_email_scripts()
    push_log(f"📬 Pending: {len(leads)}")
    base_subject=get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body   =get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY
    sent=0
    for i,lead in enumerate(leads):
        if stop_event.is_set(): break
        subj,body=ai_gen_email(lead,base_subject,base_body)
        ok=send_email(lead,subj,body)
        if ok:
            sent+=1; sheet_mark_sent(lead["app_id"],lead["email"],lead["app_name"])
            with state_lock: state["emails_sent"]=state.get("emails_sent",0)+1
        if i<len(leads)-1 and not stop_event.is_set():
            wait=random.uniform(50,100)
            for _ in range(int(wait)):
                if stop_event.is_set(): break
                time.sleep(1)
    push_log(f"✅ Sent {sent}/{len(leads)}")
    upd(running=False,phase="done")

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

@application.route("/api/start",methods=["POST"])
def api_start():
    data=request.get_json(silent=True) or {}
    kw=(data.get("keyword") or "").strip()
    if not kw: return jsonify({"error":"keyword required"}),400
    with state_lock:
        if state["running"]: return jsonify({"error":"Already running"}),409
    global run_cfg,global_seen_ids,global_seen_emails
    run_cfg=_build_run_cfg(data)
    global_seen_ids=set(); global_seen_emails=set()
    target=int(data.get("target") or os.environ.get("TARGET_LEADS",300))
    hunter=data.get("hunter") or {}
    threading.Thread(target=run_automation,args=(kw,target,hunter),daemon=True).start()
    return jsonify({"ok":True})

@application.route("/api/stop",methods=["POST"])
def api_stop():
    stop_event.set(); push_log("🛑 Stopped."); return jsonify({"ok":True})

@application.route("/api/status")
def api_status():
    with state_lock: return jsonify(dict(state))

@application.route("/api/clear",methods=["POST"])
def api_clear():
    global global_seen_ids,global_seen_emails,sheet_memory_ids,sheet_memory_emails,sheet_memory_loaded
    with state_lock:
        if state["running"]: return jsonify({"error":"Cannot clear while running"}),409
        state.update({"running":False,"phase":"idle","keyword":"","keywords_used":[],
                      "leads_found":0,"emails_sent":0,"logs":[],"leads":[],"email_script_stats":[]})
    global_seen_ids=set();global_seen_emails=set()
    sheet_memory_ids=set();sheet_memory_emails=set();sheet_memory_loaded=False
    return jsonify({"ok":True})

@application.route("/api/ping",methods=["GET","POST"])
def api_ping(): return jsonify({"ok":True,"ts":time.time()})

@application.route("/api/send_pending",methods=["POST"])
def api_send_pending():
    with state_lock:
        if state["running"]: return jsonify({"error":"Running"}),409
    data=request.get_json(silent=True) or {}
    leads=data.get("leads") or []
    if not leads: return jsonify({"error":"No leads"}),400
    global run_cfg; run_cfg=_build_run_cfg(data)
    threading.Thread(target=run_send_pending,args=(leads,),daemon=True).start()
    return jsonify({"ok":True,"count":len(leads)})

@application.route("/api/spam_test",methods=["POST"])
def api_spam_test():
    data=request.get_json(silent=True) or {}
    test_to=(data.get("test_email") or "").strip()
    if not test_to: return jsonify({"error":"test_email required"}),400
    global run_cfg; run_cfg=_build_run_cfg(data); _load_email_scripts()
    sample={"app_name":"MyFinance Pro","developer":"John Dev","category":"Finance",
            "installs":2500,"score":2.8,"email":test_to,
            "url":"https://play.google.com/store/apps/details?id=com.example","app_id":"com.example"}
    subj,body=ai_gen_email(sample,get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT,
                           get_cfg("EMAIL_BODY") or DEFAULT_EMAIL_BODY)
    ok=send_email(sample,subj,body)
    if ok: return jsonify({"ok":True,"msg":f"Sent to {test_to}","subject":subj,"body":body})
    return jsonify({"error":"Send failed -- check email script URLs"}),500

@application.route("/api/sheet_pending",methods=["POST"])
def api_sheet_pending():
    data=request.get_json(silent=True) or {}
    sheet_url=data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL","")
    if not sheet_url: return jsonify({"error":"sheet_url not set"}),400
    try:
        r=req_lib.post(sheet_url,json={"action":"get_pending"},timeout=20)
        result=r.json() if (r and r.text) else {}
        return jsonify({"ok":True,"count":len(result.get("leads",[])),"leads":result.get("leads",[])})
    except Exception as e: return jsonify({"error":str(e)}),500

@application.route("/api/proxy_status",methods=["GET","POST"])
def api_proxy_status():
    data={}
    if request.method=="POST":
        data=request.get_json(silent=True) or {}
        if data.get("proxy_list") or data.get("scraper_api_key"):
            global run_cfg
            run_cfg["PROXY_LIST"]=data.get("proxy_list","")
            run_cfg["SCRAPER_API_KEY"]=data.get("scraper_api_key","")
            refresh_proxy_pool(force=True)
    sa=get_cfg("SCRAPER_API_KEY","")
    with _proxy_lock:
        total=len(_working_proxies)
    return jsonify({"scraper_api_mode":bool(sa),"proxy_pool_total":total,"healthy":total,"retired":0})

@application.route("/api/reassign_proxies",methods=["POST"])
def api_reassign_proxies():
    with _email_script_lock: urls=list(_email_scripts)
    if not urls: return jsonify({"error":"No scripts loaded"}),400
    _assign_proxies_to_scripts(urls)
    with _script_proxy_lock:
        pairs=[{"script":i+1,"proxy":str(v)[:50] if v else "direct"}
               for i,(k,v) in enumerate(_script_proxy_map.items())]
    push_log(f"🔀 Proxies reassigned for {len(urls)} scripts")
    return jsonify({"ok":True,"pairs":pairs})

@application.route("/api/email_script_status",methods=["GET"])
def api_email_script_status():
    with _email_script_lock:
        cur=_current_script_idx%max(len(_email_scripts),1)
        stats=[{"index":i+1,"url":u[:70]+"…" if len(u)>70 else u,
                "sent":_script_sent_counts.get(u,0),"fails":_script_fail_counts.get(u,0),
                "active":i==cur,
                "status":"quota" if _script_sent_counts.get(u,0)>=MAX_SENDS_PER_SCRIPT
                          else "retired" if _script_fail_counts.get(u,0)>=MAX_SCRIPT_FAILS
                          else "active"}
               for i,u in enumerate(_email_scripts)]
    return jsonify({"scripts":stats,"total":len(_email_scripts),"max_per_script":MAX_SENDS_PER_SCRIPT})

@application.route("/api/sheet_memory_status",methods=["GET"])
def api_sheet_memory_status():
    with sheet_memory_lock:
        return jsonify({"loaded":sheet_memory_loaded,"ids":len(sheet_memory_ids),"emails":len(sheet_memory_emails)})

@application.route("/unsubscribe",methods=["GET"])
def unsubscribe():
    email=request.args.get("email","").strip().lower()
    token=request.args.get("token","").strip()
    if not email or token!=_unsub_token(email): return "<h2>Invalid link.</h2>",400
    push_log(f"📭 Unsubscribe: {email}")
    sheet_post({"action":"append","tab":"Unsubscribes","row":{"Email":email,"At":time.strftime("%Y-%m-%d %H:%M:%S")}})
    global_seen_emails.add(email); register("",email)
    return (f'<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Unsubscribed</title>'
            f'<style>body{{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;'
            f'min-height:100vh;margin:0;background:#f9f9f9;}}'
            f'.box{{background:#fff;padding:40px;border-radius:8px;text-align:center;max-width:400px;'
            f'box-shadow:0 1px 4px rgba(0,0,0,.1);}}h2{{color:#222;}}p{{color:#555;}}</style></head>'
            f'<body><div class="box"><h2>Unsubscribed</h2>'
            f'<p>{email} removed. No more emails from PlayReview.</p></div></body></html>')

if __name__=="__main__":
    application.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=False)
