import os, time, random, threading, json, re, logging
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
# app_id এবং email lowercase — দুটোই এখানে track হয়
global_seen_ids:    set = set()
global_seen_emails: set = set()

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

# ── Google Sheet via Apps Script ──────────────────────────────────────────────
def sheet_post(payload: dict):
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        return
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        push_log(f"  Sheet error: {e}")

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

# ── Sheet-based duplicate loader ──────────────────────────────────────────────
def load_sheet_duplicates():
    """
    প্রতিটা automation run শুরুর আগে একবার call হয়।
    Sheet এর 'All Leads' tab থেকে সব existing App ID + Email
    global_seen_ids / global_seen_emails এ load করে।

    এরপর scrape_keyword() এ কোনো নতুন app এই দুই set এর বিরুদ্ধে
    check হয় — match হলে সেটা duplicate হিসেবে skip হয়।

    IMPORTANT:
      - App ID: exact match (case-sensitive, Play Store format)
      - Email: lowercase করে store ও compare — case mismatch এ duplicate miss হতো
      - Sheet এ App ID নেই এমন row email দিয়েও catch হবে
    """
    global global_seen_ids, global_seen_emails
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        push_log("  ⚠ No sheet URL configured — duplicate check is in-memory only.")
        return

    push_log("  📋 Loading sheet leads for duplicate prevention...")
    try:
        r = requests.post(url, json={"action": "get_all_leads"}, timeout=35)
        if not r.text:
            push_log("  ⚠ Sheet returned empty response — skipping dedup load.")
            return
        result = r.json()
        leads  = result.get("leads", [])
        if not leads:
            push_log("  Sheet has 0 existing leads — starting fresh.")
            return

        added_ids    = 0
        added_emails = 0
        for lead in leads:
            # App ID — try both key formats (sheet returns app_id or App ID)
            aid = (
                lead.get("app_id") or
                lead.get("App ID") or ""
            ).strip()

            # Email — always lowercase for consistent comparison
            em = (
                lead.get("email") or
                lead.get("Email") or ""
            ).strip().lower()

            if aid and aid not in global_seen_ids:
                global_seen_ids.add(aid)
                added_ids += 1

            if em and em not in global_seen_emails:
                global_seen_emails.add(em)
                added_emails += 1

        push_log(
            f"  ✅ Sheet dedup loaded: {added_ids} app IDs + "
            f"{added_emails} emails blocked from re-scraping."
        )

    except Exception as e:
        push_log(f"  ❌ Sheet dedup load failed: {e} — in-memory dedup only.")

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

# ── Dual template defaults ────────────────────────────────────────────────────
DEFAULT_NEW_APP_SUBJECT = "Quick question about {{app_name}}"
DEFAULT_NEW_APP_BODY = """Hi {{developer}} team,

I came across {{app_name}} on Google Play — a {{category}} app that's still in its early growth phase with {{installs}} installs.

As a new app, building a strong reputation from day one is critical. I run a Play Store growth service that helps developers like you boost visibility, gather early positive reviews, and establish credibility before the competition catches on.

Would you be open to a quick 15-minute chat this week?

Best regards,
{{sender_name}}
{{sender_company}}

App: {{url}}"""

DEFAULT_OLD_APP_SUBJECT = "Noticed {{app_name}}'s {{score}}★ rating — quick idea"
DEFAULT_OLD_APP_BODY = """Hi {{developer}} team,

I came across {{app_name}} on Google Play and noticed it currently holds a {{score}}★ rating in the {{category}} category with {{installs}} installs.

A rating in this range often means there are fixable issues hurting your reputation. I run a Play Store review recovery service that helps developers like you quickly clean up rating problems, respond to bad reviews professionally, and turn things around before it impacts downloads.

Would you be open to a quick 15-minute chat this week?

Best regards,
{{sender_name}}
{{sender_company}}

App: {{url}}"""

# Legacy alias (backward compat)
DEFAULT_EMAIL_SUBJECT = DEFAULT_NEW_APP_SUBJECT
DEFAULT_EMAIL_BODY    = DEFAULT_NEW_APP_BODY

# ── Template helpers ──────────────────────────────────────────────────────────
def format_score(score) -> str:
    """Return formatted score string, or empty string if no valid score."""
    if score is None or score == "" or score == 0:
        return ""
    try:
        val = float(score)
        return f"{val:.1f}" if val > 0 else ""
    except:
        return ""

def select_template(lead: dict) -> tuple[str, str]:
    """
    Lead এর score দেখে সঠিক template বেছে দেয়।
    Score আছে (> 0)  → OLD APP template (struggling/rated app)
    Score নেই / 0    → NEW APP template (brand new, no reviews)
    """
    has_rating = bool(format_score(lead.get("score")))
    if has_rating:
        subject = get_cfg("OLD_APP_EMAIL_SUBJECT") or DEFAULT_OLD_APP_SUBJECT
        body    = get_cfg("OLD_APP_EMAIL_BODY")    or DEFAULT_OLD_APP_BODY
    else:
        subject = get_cfg("NEW_APP_EMAIL_SUBJECT") or DEFAULT_NEW_APP_SUBJECT
        body    = get_cfg("NEW_APP_EMAIL_BODY")    or DEFAULT_NEW_APP_BODY
    return subject, body

def fill_template(tpl: str, lead: dict) -> str:
    sender_name    = get_cfg("SENDER_NAME", "Your Name")
    sender_company = get_cfg("SENDER_COMPANY", "Your Company")
    score_str      = format_score(lead.get("score")) or "N/A"
    try:
        installs_str = f"{int(lead.get('installs', 0)):,}"
    except:
        installs_str = str(lead.get("installs", ""))
    filled = (tpl
        .replace("{{app_name}}",       lead.get("app_name", ""))
        .replace("{{developer}}",      lead.get("developer", ""))
        .replace("{{category}}",       lead.get("category", ""))
        .replace("{{installs}}",       installs_str)
        .replace("{{score}}",          score_str)
        .replace("{{url}}",            lead.get("url", ""))
        .replace("{{sender_name}}",    sender_name)
        .replace("{{sender_company}}", sender_company)
    )
    # Remove any leftover placeholders
    return re.sub(r"\{\{[a-zA-Z_]+\}\}", "", filled)

# ── AI email generation (dual-template aware) ─────────────────────────────────
def ai_gen_email(lead: dict, base_subject: str = "", base_body: str = "") -> tuple[str, str]:
    """
    Dual-mode: automatically picks NEW APP or OLD APP template based on score.
    base_subject / base_body params are ignored — template is chosen by select_template().
    Falls back to plain fill_template() if no Groq key or AI fails.
    """
    tpl_subject, tpl_body = select_template(lead)
    key = get_cfg("GROQ_API_KEY")

    if not key:
        return fill_template(tpl_subject, lead), fill_template(tpl_body, lead)

    client       = Groq(api_key=key)
    score_fmt    = format_score(lead.get("score"))
    score_info   = f"{score_fmt} stars" if score_fmt else "no ratings yet (brand new)"
    install_info = f"{lead['installs']:,} installs" if lead.get("installs") else "just launched"
    ttype        = "OLD APP (has rating)" if score_fmt else "NEW APP (no rating)"

    prompt = f"""You are a cold email personalizer. Your only job is to fill in the base template with the real app details — keeping the structure and wording almost identical.

TEMPLATE TYPE: {ttype}

BASE TEMPLATE (follow this EXACTLY):
Subject: {tpl_subject}
Body:
{tpl_body}

APP DETAILS:
- App Name: {lead.get('app_name', '')}
- Developer: {lead.get('developer', '')}
- Category: {lead.get('category', '')}
- Installs: {install_info}
- Rating: {score_info}
- Play Store URL: {lead.get('url', '')}

SENDER:
- Name: {get_cfg("SENDER_NAME", "Your Name")}
- Company: {get_cfg("SENDER_COMPANY", "Your Company")}

STRICT RULES:
1. Copy the template EXACTLY — same structure, same sentences, same flow
2. Only replace placeholder values with real app details above
3. You may change at most 2-3 words in the entire body to naturally fit this specific app
4. Do NOT rewrite, add, or remove any sentences
5. Do NOT change the greeting format, CTA, or sign-off
6. NEVER leave any {{{{variable}}}} placeholder in the output — replace them all
7. Preserve every line break and blank line from the template exactly
8. Return ONLY valid JSON: {{"subject":"...","body":"..."}} — no markdown, no explanation"""

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=600
        )
        raw  = re.sub(r"```[a-z]*", "", resp.choices[0].message.content.strip())
        raw  = raw.replace("```", "").strip()
        data = json.loads(raw)
        subject = re.sub(r"\{\{[a-zA-Z_]+\}\}", "",
                         data.get("subject") or fill_template(tpl_subject, lead))
        body    = re.sub(r"\{\{[a-zA-Z_]+\}\}", "",
                         (data.get("body") or fill_template(tpl_body, lead)).replace("\\n", "\n"))
        return subject, body
    except Exception as e:
        push_log(f"  AI email error (using template fallback): {e}")
        return fill_template(tpl_subject, lead), fill_template(tpl_body, lead)

# ── Play Store scraper ────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

SEARCH_COMBOS = [
    ("en", "us"), ("en", "gb"), ("en", "in"), ("en", "au"), ("en", "ca"),
]

def extract_email(text):
    if not text:
        return ""
    m = EMAIL_RE.search(str(text))
    return m.group(0) if m else ""

def passes_filter(installs: int, score, ratings_count: int, hunter: dict) -> bool:
    """
    ─────────────────────────────────────────────────────────────
    NORMAL MODE  (hunter inactive)
      Target: Brand-new apps with ZERO rating and ZERO reviews.
      ✅ score must be None OR exactly 0  (no published rating)
      ✅ ratings_count must be 0          (no reviews at all)
      ✅ installs: 500 – 5,000
      ❌ Any published rating → reject
      ❌ Any review → reject (confirms app is not brand-new)

    HUNTER MODE  (hunter active)
      Target: Struggling apps with a LOW published rating.
      ✅ score must be > 0                (has a real published rating)
      ✅ score ≤ max_score                (rating is low)
      ✅ ratings_count ≥ 1               (confirms rating is real)
      ✅ installs: 50 – max_installs
      ❌ No rating → reject (those go to normal mode)
    ─────────────────────────────────────────────────────────────
    The two modes never overlap:
      Unrated apps  → normal mode only
      Rated apps    → hunter mode only
    """
    is_hunter = bool(hunter and hunter.get("active"))

    if is_hunter:
        # ── HUNTER: must have a real, low rating ──────────────────────────────
        if score is None:
            return False                        # no rating → normal mode territory
        try:
            s = float(score)
        except (TypeError, ValueError):
            return False
        if s <= 0:
            return False                        # treat 0 as unrated
        if int(ratings_count or 0) < 1:
            return False                        # rating with 0 reviews is bogus

        max_inst  = int(hunter.get("max_installs") or 5000)
        max_score = float(hunter.get("max_score")  or 2.5)
        if s > max_score:
            return False                        # rating too high — not struggling
        inst = int(installs or 0)
        if inst < 50 or inst > max_inst:
            return False
        return True

    else:
        # ── NORMAL: must have NO rating and NO reviews ─────────────────────────
        if score is not None:
            try:
                if float(score) > 0:
                    return False                # has a real rating → hunter mode
            except (TypeError, ValueError):
                pass
        if int(ratings_count or 0) > 0:
            return False                        # has reviews → not brand-new
        inst = int(installs or 0)
        if inst < 500 or inst > 5_000:
            return False
        return True

def scrape_keyword(keyword: str, hunter: dict = None) -> list:
    """
    একটা keyword এ সব SEARCH_COMBOS country তে search করে leads collect করে।

    Duplicate prevention (3 layer):
      1. global_seen_ids   — এই run + sheet এর সব existing app IDs
      2. global_seen_emails — এই run + sheet এর সব existing emails (lowercase)
      3. passes_filter()   — mode-specific install/rating/review filter
    """
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

            app_id = (item.get("appId") or "").strip()
            if not app_id:
                continue

            # ── Layer 1: App ID duplicate check (before fetching detail) ──────
            if app_id in global_seen_ids:
                continue

            # ── Fetch full detail ─────────────────────────────────────────────
            try:
                details = gp_app(app_id, lang="en", country="us")
            except Exception:
                # Can't fetch → mark seen so we don't retry this run
                global_seen_ids.add(app_id)
                continue

            installs      = int(details.get("minInstalls") or 0)
            score         = details.get("score")           # float | None
            ratings_count = int(details.get("ratings") or 0)

            # ── Layer 2: Mode filter (normal vs hunter) ───────────────────────
            if not passes_filter(installs, score, ratings_count, hunter):
                # Mark seen so same app isn't rechecked in this run
                global_seen_ids.add(app_id)
                continue

            # ── Email extraction ──────────────────────────────────────────────
            email = (
                extract_email(details.get("developerEmail", ""))
                or extract_email(details.get("privacyPolicy", ""))
                or extract_email(details.get("description", ""))
                or extract_email(details.get("recentChanges", ""))
            )
            if not email:
                global_seen_ids.add(app_id)
                continue

            email_lc = email.strip().lower()

            # ── Layer 3: Email duplicate check ────────────────────────────────
            if email_lc in global_seen_emails:
                global_seen_ids.add(app_id)
                continue

            # ── All checks passed → qualified lead ────────────────────────────
            global_seen_ids.add(app_id)
            global_seen_emails.add(email_lc)

            lead = {
                "app_id":      app_id,
                "app_name":    details.get("title", ""),
                "developer":   (details.get("developer") or "").strip(),
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

            mode_tag  = "HUNTER" if (hunter and hunter.get("active")) else "NORMAL"
            score_str = f"{score:.1f}★" if score else "no-rating"
            push_log(
                f"  ✅ [{mode_tag}] {lead['app_name']} "
                f"| {installs:,} inst | {score_str} "
                f"| {ratings_count} reviews | {email}"
            )
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
            return True
        push_log(f"  ❌ Email failed: {lead['email']}: {result.get('msg','?')}")
        return False
    except Exception as e:
        push_log(f"  ❌ Email error: {e}")
        return False

# ── Master automation ─────────────────────────────────────────────────────────
def run_automation(initial_kw: str, target: int, hunter: dict = None):
    upd(running=True, phase="scraping", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode}")

    # ── Sheet থেকে existing leads load → duplicate prevention ─────────────────
    load_sheet_duplicates()

    all_leads = []
    kws_used  = [initial_kw]
    kw_queue  = [initial_kw]

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

        ttype = "OLD APP" if format_score(lead.get("score")) else "NEW APP"
        push_log(f"  🤖 AI writing email for {lead['app_name']} [{ttype}] …")
        subject, body = ai_gen_email(lead)

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
    sent = 0
    for i, lead in enumerate(leads):
        if stop_event.is_set():
            push_log("🛑 Stopped.")
            break
        ttype = "OLD APP" if format_score(lead.get("score")) else "NEW APP"
        push_log(f"  🤖 AI writing email for {lead.get('app_name','')} [{ttype}] …")
        subject, body = ai_gen_email(lead)
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
        "GROQ_API_KEY":          data.get("groq_key")              or os.environ.get("GROQ_API_KEY", ""),
        "APPS_SCRIPT_WEB_URL":   data.get("sheet_url")             or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "EMAIL_SCRIPT_URL":      data.get("email_script_url")      or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":           data.get("sender_name")           or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":        data.get("sender_company")        or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":         data.get("email_subject")         or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":            data.get("email_body")            or os.environ.get("EMAIL_BODY", ""),
        # Dual-template
        "NEW_APP_EMAIL_SUBJECT": data.get("new_app_email_subject") or os.environ.get("NEW_APP_EMAIL_SUBJECT", ""),
        "NEW_APP_EMAIL_BODY":    data.get("new_app_email_body")    or os.environ.get("NEW_APP_EMAIL_BODY", ""),
        "OLD_APP_EMAIL_SUBJECT": data.get("old_app_email_subject") or os.environ.get("OLD_APP_EMAIL_SUBJECT", ""),
        "OLD_APP_EMAIL_BODY":    data.get("old_app_email_body")    or os.environ.get("OLD_APP_EMAIL_BODY", ""),
    }
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
    global global_seen_ids, global_seen_emails
    with state_lock:
        if state["running"]:
            return jsonify({"error": "Cannot clear while running"}), 409
        state.update({
            "running": False, "phase": "idle", "keyword": "",
            "keywords_used": [], "leads_found": 0, "emails_sent": 0,
            "logs": [], "leads": []
        })
    global_seen_ids    = set()
    global_seen_emails = set()
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
        "GROQ_API_KEY":          data.get("groq_key")              or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL":      data.get("email_script_url")      or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":           data.get("sender_name")           or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":        data.get("sender_company")        or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":         data.get("email_subject")         or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":            data.get("email_body")            or os.environ.get("EMAIL_BODY", ""),
        "APPS_SCRIPT_WEB_URL":   data.get("sheet_url")             or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        # Dual-template
        "NEW_APP_EMAIL_SUBJECT": data.get("new_app_email_subject") or os.environ.get("NEW_APP_EMAIL_SUBJECT", ""),
        "NEW_APP_EMAIL_BODY":    data.get("new_app_email_body")    or os.environ.get("NEW_APP_EMAIL_BODY", ""),
        "OLD_APP_EMAIL_SUBJECT": data.get("old_app_email_subject") or os.environ.get("OLD_APP_EMAIL_SUBJECT", ""),
        "OLD_APP_EMAIL_BODY":    data.get("old_app_email_body")    or os.environ.get("OLD_APP_EMAIL_BODY", ""),
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
        "GROQ_API_KEY":          data.get("groq_key")              or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL":      data.get("email_script_url")      or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":           data.get("sender_name")           or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":        data.get("sender_company")        or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":         data.get("email_subject")         or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":            data.get("email_body")            or os.environ.get("EMAIL_BODY", ""),
        # Dual-template
        "NEW_APP_EMAIL_SUBJECT": data.get("new_app_email_subject") or os.environ.get("NEW_APP_EMAIL_SUBJECT", ""),
        "NEW_APP_EMAIL_BODY":    data.get("new_app_email_body")    or os.environ.get("NEW_APP_EMAIL_BODY", ""),
        "OLD_APP_EMAIL_SUBJECT": data.get("old_app_email_subject") or os.environ.get("OLD_APP_EMAIL_SUBJECT", ""),
        "OLD_APP_EMAIL_BODY":    data.get("old_app_email_body")    or os.environ.get("OLD_APP_EMAIL_BODY", ""),
    }
    raw_score    = data.get("sample_score")
    sample_score = float(raw_score) if raw_score is not None and raw_score != "" else None
    sample = {
        "app_name":  data.get("sample_app_name", "MyApp Pro"),
        "developer": data.get("sample_developer", "John Dev"),
        "category":  "Productivity",
        "installs":  1500,
        "score":     sample_score,
        "email":     test_to,
        "url":       "https://play.google.com/store/apps/details?id=com.example",
    }
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url:
        return jsonify({"error": "EMAIL_SCRIPT_URL not set"}), 400
    ttype = "OLD APP" if format_score(sample_score) else "NEW APP"
    push_log(f"  Spam test: {ttype} template (score={sample_score})")
    subject, body = ai_gen_email(sample)
    try:
        r      = requests.post(url, json={"to": test_to, "subject": subject, "body": body}, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            return jsonify({
                "ok": True, "msg": f"Test sent to {test_to}",
                "template_type": ttype, "subject": subject, "body": body,
            })
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

# ── Sheet all leads fetch (for DB tab sync) ───────────────────────────────────
@application.route("/api/sheet_all", methods=["POST"])
def api_sheet_all():
    data      = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400
    try:
        r      = requests.post(sheet_url, json={"action": "get_all_leads"}, timeout=25)
        result = r.json() if r.text else {}
        return jsonify({"ok": True, "leads": result.get("leads", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
