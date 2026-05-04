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
global_seen_ids: set = set()
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

# ── [FIX 1] Pre-run sheet loader ──────────────────────────────────────────────
# Fetches every App ID and Email already stored in the "All Leads" tab and
# loads them into global_seen_ids / global_seen_emails BEFORE scraping starts.
#
# FIX: The original version only tried POST. Many Apps Script deployments only
# handle doGet(), so a POST silently fails and returns an empty/error response,
# causing the pre-load to return empty sets — making the duplicate guard useless.
#
# This version:
#   1. Tries POST with JSON body first (works if Apps Script has doPost).
#   2. If POST returns empty sets, falls back to GET with ?action=get_all_ids
#      (works if Apps Script only has doGet).
#   3. Logs the raw response so you can see exactly what the sheet is returning.
# ─────────────────────────────────────────────────────────────────────────────
def load_sheet_existing_ids_and_emails() -> tuple[set, set]:
    """One-shot fetch of all App IDs and Emails already in the sheet.

    Returns (seen_ids, seen_emails). Falls back to empty sets on any failure
    so scraping proceeds normally (fail-open). Tries POST then GET.
    """
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        push_log("⚠️  Sheet pre-load skipped: APPS_SCRIPT_WEB_URL not set.")
        return set(), set()

    def _parse(resp) -> tuple[set, set]:
        """Parse a requests.Response into (seen_ids, seen_emails)."""
        push_log(f"  Sheet pre-load raw response [{resp.status_code}]: {resp.text[:400]}")
        try:
            data = resp.json() if resp.text.strip() else {}
        except Exception:
            data = {}
        raw_ids    = data.get("app_ids") or []
        raw_emails = data.get("emails")  or []
        seen_ids    = {str(i).strip()         for i in raw_ids    if i}
        seen_emails = {str(e).strip().lower() for e in raw_emails if e}
        return seen_ids, seen_emails

    # ── Attempt 1: POST with JSON body ────────────────────────────────────────
    try:
        resp = requests.post(url, json={"action": "get_all_ids"}, timeout=30)
        seen_ids, seen_emails = _parse(resp)
        if seen_ids or seen_emails:
            push_log(
                f"📋 Sheet pre-load (POST): {len(seen_ids)} app IDs, "
                f"{len(seen_emails)} emails loaded into memory."
            )
            return seen_ids, seen_emails
        push_log("  POST returned empty sets — trying GET fallback …")
    except Exception as e:
        push_log(f"  POST attempt failed: {e} — trying GET fallback …")

    # ── Attempt 2: GET with query string ──────────────────────────────────────
    try:
        resp = requests.get(url, params={"action": "get_all_ids"}, timeout=30)
        seen_ids, seen_emails = _parse(resp)
        push_log(
            f"📋 Sheet pre-load (GET): {len(seen_ids)} app IDs, "
            f"{len(seen_emails)} emails loaded into memory."
        )
        return seen_ids, seen_emails
    except Exception as e:
        push_log(f"⚠️  Sheet pre-load GET also failed: {e}. Proceeding without pre-load.")
        return set(), set()
# ── End pre-run sheet loader ──────────────────────────────────────────────────

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
def ai_gen_email(
    lead: dict,
    base_subject: str,
    base_body: str,
    new_app_subject: str = "",
    new_app_body: str    = "",
) -> tuple[str, str]:
    if new_app_subject and new_app_body and is_new_app(lead):
        push_log(f"  📧 Using NEW APP template for {lead.get('app_name','')}")
        base_subject = new_app_subject
        base_body    = new_app_body
    else:
        push_log(f"  📧 Using OLD APP template for {lead.get('app_name','')}")

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

DEFAULT_NEW_APP_SUBJECT = "Congrats on launching {{app_name}} 🚀"
DEFAULT_NEW_APP_BODY = """Hi {{developer}} team,

I just discovered {{app_name}} on Google Play — looks like you've recently launched and are still building your first audience. Exciting stage!

I help new app developers like you get their first wave of real, organic reviews and ratings so they can gain trust quickly and climb the Play Store rankings.

Would you be interested in a quick chat to see if I can help {{app_name}} get off the ground faster?

Best regards,
{{sender_name}}
{{sender_company}}

App: {{url}}"""

NEW_APP_MAX_INSTALLS = 1_000

def is_new_app(lead: dict) -> bool:
    score    = lead.get("score")
    installs = lead.get("installs") or 0
    has_rating = score is not None and score > 0
    if installs >= NEW_APP_MAX_INSTALLS:
        return False
    return not has_rating

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

# ── Play Store scraper ────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

SEARCH_COMBOS = [
    ("en", "us"), ("en", "gb"), ("en", "in"), ("en", "au"), ("en", "ca"),
]

RATING_FETCH_COUNTRIES = ["us", "gb", "in", "au", "ca"]

def fetch_app_details_with_rating(app_id: str) -> dict:
    first_result = None
    for country in RATING_FETCH_COUNTRIES:
        try:
            details = gp_app(app_id, lang="en", country=country)
        except Exception:
            continue

        if first_result is None:
            first_result = details

        score = details.get("score")
        if score and score > 0:
            if country != RATING_FETCH_COUNTRIES[0]:
                log.info(
                    f"  🌍 Rating resolved via country='{country}' "
                    f"for {app_id}: {score:.1f}★"
                )
            return details

    return first_result or {}

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
        if score is not None and score > max_score:
            return False
        return True
    if installs > 10_000:
        return False
    if score is not None and score > 3.5:
        return False
    return True

def scrape_keyword(keyword: str, hunter: dict = None) -> list:
    """Scrape across multiple country combos; deduplicate globally."""
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
            try:
                details = fetch_app_details_with_rating(app_id)
                if not details:
                    raise ValueError("empty details")
            except Exception:
                global_seen_ids.add(app_id)
                continue

            installs = details.get("minInstalls") or 0
            score    = details.get("score")

            if not passes_filter(installs, score, hunter):
                global_seen_ids.add(app_id)
                continue

            email = (
                extract_email(details.get("developerEmail", ""))
                or extract_email(details.get("privacyPolicy", ""))
                or extract_email(details.get("description", ""))
                or extract_email(details.get("recentChanges", ""))
            )
            if not email or email in global_seen_emails:
                global_seen_ids.add(app_id)
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
            score_str = f"{score:.1f}★" if score else "new"
            push_log(f"  ✅ {lead['app_name']} | {installs:,} installs | {score_str} | {email}")
            time.sleep(0.25)

        push_log(f"  [{country}] done. Leads so far: {len(leads)}")
        time.sleep(0.5)

    leads.sort(key=lambda l: (l["score"] is None or l["score"] == 0, 0))
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

# ── [FIX 2] Sheet leads loader for /api/status ────────────────────────────────
# The dashboard was showing 0 leads because state["leads"] only contains leads
# found in the CURRENT run. Leads from previous runs exist only in the sheet.
# This helper fetches all leads from the sheet so the dashboard can display them
# even when the automation is idle (phase="idle" or phase="done").
# Called by api_status() when state["leads"] is empty and automation is not running.
# ─────────────────────────────────────────────────────────────────────────────
def fetch_leads_from_sheet() -> list:
    """Fetch all leads from the sheet's 'All Leads' tab for display.

    Returns a list of lead dicts, or [] on any failure.
    Apps Script must handle action="get_all_leads" and return:
      { "leads": [ { "App ID": ..., "App Name": ..., "Email": ..., ... }, ... ] }

    Tries POST then GET (same fallback logic as load_sheet_existing_ids_and_emails).
    """
    url = get_cfg("APPS_SCRIPT_WEB_URL")
    if not url:
        return []

    def _do_request(method: str) -> list:
        if method == "POST":
            resp = requests.post(url, json={"action": "get_all_leads"}, timeout=30)
        else:
            resp = requests.get(url, params={"action": "get_all_leads"}, timeout=30)
        data = resp.json() if resp.text.strip() else {}
        raw = data.get("leads") or []
        # Normalise sheet column names → internal lead dict keys
        leads = []
        for r in raw:
            leads.append({
                "app_id":     r.get("App ID", ""),
                "app_name":   r.get("App Name", ""),
                "developer":  r.get("Developer", ""),
                "email":      r.get("Email", ""),
                "category":   r.get("Category", ""),
                "installs":   r.get("Installs", 0),
                "score":      r.get("Score") or None,
                "url":        r.get("URL", ""),
                "keyword":    r.get("Keyword", ""),
                "scraped_at": r.get("Scraped At", ""),
                "email_sent": r.get("Email Sent", "No") not in ("No", "", None),
                "icon":       "",
                "description": "",
            })
        return leads

    for method in ("POST", "GET"):
        try:
            leads = _do_request(method)
            if leads:
                return leads
        except Exception as e:
            push_log(f"  fetch_leads_from_sheet {method} failed: {e}")

    return []
# ── End sheet leads loader ────────────────────────────────────────────────────

# ── Master automation ─────────────────────────────────────────────────────────
def run_automation(initial_kw: str, target: int, hunter: dict = None):
    upd(running=True, phase="scraping", keyword=initial_kw,
        keywords_used=[], leads_found=0, emails_sent=0, logs=[], leads=[])
    stop_event.clear()
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode}")

    push_log("📋 Pre-loading existing leads from sheet …")
    sheet_ids, sheet_emails = load_sheet_existing_ids_and_emails()
    global global_seen_ids, global_seen_emails
    global_seen_ids    |= sheet_ids
    global_seen_emails |= sheet_emails

    base_subject = get_cfg("EMAIL_SUBJECT") or DEFAULT_EMAIL_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or DEFAULT_EMAIL_BODY
    new_app_subject = get_cfg("NEW_APP_EMAIL_SUBJECT") or DEFAULT_NEW_APP_SUBJECT
    new_app_body    = get_cfg("NEW_APP_EMAIL_BODY")    or DEFAULT_NEW_APP_BODY

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

        push_log(f"  🤖 AI writing email for {lead['app_name']} …")
        subject, body = ai_gen_email(lead, base_subject, base_body, new_app_subject, new_app_body)

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
    new_app_subject = get_cfg("NEW_APP_EMAIL_SUBJECT") or DEFAULT_NEW_APP_SUBJECT
    new_app_body    = get_cfg("NEW_APP_EMAIL_BODY")    or DEFAULT_NEW_APP_BODY
    sent = 0
    for i, lead in enumerate(leads):
        if stop_event.is_set():
            push_log("🛑 Stopped.")
            break
        push_log(f"  🤖 AI writing email for {lead.get('app_name','')} …")
        subject, body = ai_gen_email(lead, base_subject, base_body, new_app_subject, new_app_body)
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
        "GROQ_API_KEY":           data.get("groq_key")              or os.environ.get("GROQ_API_KEY", ""),
        "APPS_SCRIPT_WEB_URL":    data.get("sheet_url")             or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "EMAIL_SCRIPT_URL":       data.get("email_script_url")      or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":            data.get("sender_name")           or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":         data.get("sender_company")        or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":          data.get("email_subject")         or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":             data.get("email_body")            or os.environ.get("EMAIL_BODY", ""),
        "NEW_APP_EMAIL_SUBJECT":  data.get("new_app_email_subject") or os.environ.get("NEW_APP_EMAIL_SUBJECT", ""),
        "NEW_APP_EMAIL_BODY":     data.get("new_app_email_body")    or os.environ.get("NEW_APP_EMAIL_BODY", ""),
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

# ── [FIX 2] /api/status — load sheet leads when idle ─────────────────────────
# Previously, state["leads"] was always empty when the automation wasn't running
# (idle/done/stopped), making the dashboard show 0 leads even when the sheet
# had hundreds of existing entries.
#
# Fix: when not running AND state["leads"] is empty, fetch leads from the sheet
# and populate state["leads"] so the dashboard can display them.
# The sheet_url is read from the request query param or from the environment.
# ─────────────────────────────────────────────────────────────────────────────
@application.route("/api/status")
def api_status():
    with state_lock:
        snap = dict(state)

    # If idle and no in-memory leads, try fetching from sheet for display
    if not snap["running"] and not snap["leads"]:
        sheet_url = request.args.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
        if sheet_url:
            # Temporarily set run_cfg so get_cfg("APPS_SCRIPT_WEB_URL") resolves
            global run_cfg
            original_cfg = dict(run_cfg)
            run_cfg = {**run_cfg, "APPS_SCRIPT_WEB_URL": sheet_url}
            sheet_leads = fetch_leads_from_sheet()
            run_cfg = original_cfg
            if sheet_leads:
                snap["leads"] = sheet_leads
                snap["leads_found"] = len(sheet_leads)

    return jsonify(snap)
# ── End status fix ────────────────────────────────────────────────────────────

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
        "GROQ_API_KEY":           data.get("groq_key")              or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL":       data.get("email_script_url")      or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":            data.get("sender_name")           or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":         data.get("sender_company")        or os.environ.get("SENDER_COMPANY", ""),
        "EMAIL_SUBJECT":          data.get("email_subject")         or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":             data.get("email_body")            or os.environ.get("EMAIL_BODY", ""),
        "APPS_SCRIPT_WEB_URL":    data.get("sheet_url")             or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "NEW_APP_EMAIL_SUBJECT":  data.get("new_app_email_subject") or os.environ.get("NEW_APP_EMAIL_SUBJECT", ""),
        "NEW_APP_EMAIL_BODY":     data.get("new_app_email_body")    or os.environ.get("NEW_APP_EMAIL_BODY", ""),
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
