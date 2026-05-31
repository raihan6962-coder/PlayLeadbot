"""
main.py — PlayLead Engine v2.1
Flask entry point. All business logic lives in modules/.
"""

import os


import json
import time
import threading
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import app_config as cfg
from app_config import set_run_cfg, get_cfg
import app_state as sm
import app_sheets as sheets
import app_scraper as scraper
import app_keywords as kw_eng
import app_email as email_eng

# ── Flask setup ───────────────────────────────────────────────────────────────
application = Flask(__name__, static_folder=os.path.dirname(os.path.abspath(__file__)))
app = application
CORS(application)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(module)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Keep-alive self-ping ──────────────────────────────────────────────────────
def _keepalive():
    time.sleep(60)
    while True:
        try:
            host = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") and
                "https://" + os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")) or \
               os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
            import requests as _req
            _req.get(f"{host}/api/ping", timeout=10)
        except Exception:
            pass
        time.sleep(840)  # every 14 minutes

threading.Thread(target=_keepalive, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Core automation
# ─────────────────────────────────────────────────────────────────────────────

def run_automation(initial_kw: str, target: int, hunter: dict):
    mode = "Hunter" if (hunter and hunter.get("active")) else "Normal"
    sm.upd(running=True, phase="loading_sheet", keyword=initial_kw,
           mode=mode.lower(), keywords_used=[], leads_found=0,
           emails_sent=0, emails_failed=0, leads=[], logs=[])
    sm.stop_event.clear()
    sm.clear_dedup()

    sm.push_log(f"🚀 Started | kw='{initial_kw}' | target={target} | mode={mode}")

    # ── Step 0: Load sheet memory ─────────────────────────────────────────────
    sm.push_log("📋 Loading existing sheet records …")
    try:
        ids, emails = sheets.sheet_load_memory()
        sm.load_sheet_memory(ids, emails)
        sm.push_log(f"   Memory: {len(ids)} IDs, {len(emails)} emails already in sheet")
    except Exception as e:
        sm.push_log(f"⚠️  Sheet memory load failed: {e} — continuing")
        sm.load_sheet_memory(set(), set())

    base_subject = get_cfg("EMAIL_SUBJECT") or email_eng.DEFAULT_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or email_eng.DEFAULT_BODY

    all_leads: list = []
    kws_used:  list = [initial_kw]
    kw_queue:  list = [initial_kw]

    sm.upd(phase="scraping")

    # ── Phase 1: Scrape ───────────────────────────────────────────────────────
    while len(all_leads) < target and not sm.stop_event.is_set():
        if not kw_queue:
            sm.push_log("🤖 Requesting AI keywords …")
            new_kws = kw_eng.ai_gen_keywords(initial_kw, kws_used)
            if not new_kws:
                sm.push_log("⚠️  No more keywords available. Stopping scrape.")
                break
            kw_queue.extend(new_kws)

        kw = kw_queue.pop(0)
        if kw not in kws_used:
            kws_used.append(kw)
        sm.upd(keywords_used=kws_used[:], phase="scraping")

        batch = scraper.scrape_keyword(kw, hunter, sm.stop_event)
        all_leads.extend(batch)
        sm.upd(leads_found=len(all_leads), leads=[l.copy() for l in all_leads])

        for lead in batch:
            sheets.sheet_append_lead(lead)
            sheets.sheet_append_qualified(lead)

        sm.push_log(f"📊 Total: {len(all_leads)} / {target}")

    if sm.stop_event.is_set():
        sm.push_log("🛑 Stopped during scraping.")
        sm.upd(running=False, phase="stopped")
        return

    sm.push_log(f"✅ Scraping done. {len(all_leads)} leads. Starting emails …")

    # ── Phase 2: AI Email + Send ──────────────────────────────────────────────
    sm.upd(phase="emailing")
    send_profile = email_eng.get_send_profile()

    for i, lead in enumerate(all_leads):
        if sm.stop_event.is_set():
            sm.push_log("🛑 Stopped during email phase.")
            break

        sm.push_log(f"  🤖 AI writing email {i+1}/{len(all_leads)}: {lead['app_name']}")
        subject, body = email_eng.ai_rewrite_email(lead, base_subject, base_body)

        # Spam check before send
        check = email_eng.spam_score(subject, body)
        if check["score"] >= 70:
            sm.push_log(f"  ❌ Skip (spam score {check['score']}): {lead['email']}")
            sm.inc_failed()
            continue

        ok = _send_email(lead, subject, body)
        lead["email_sent"] = ok

        if ok:
            sm.inc_sent()
            sheets.sheet_mark_sent(lead["app_id"], lead["email"], lead["app_name"])
        else:
            sm.inc_failed()

        sm.upd(leads=[l.copy() for l in all_leads])

        if i < len(all_leads) - 1 and not sm.stop_event.is_set():
            email_eng.humanized_delay(
                sm.stop_event, profile=send_profile,
                lead_idx=i, total=len(all_leads)
            )

    if sm.stop_event.is_set():
        sm.upd(running=False, phase="stopped")
    else:
        sm.push_log("🎉 Automation complete!")
        sm.upd(running=False, phase="done")


def run_send_pending(leads: list):
    sm.upd(running=True, phase="emailing")
    sm.stop_event.clear()
    sm.push_log(f"📬 Send pending: {len(leads)} leads")

    base_subject = get_cfg("EMAIL_SUBJECT") or email_eng.DEFAULT_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or email_eng.DEFAULT_BODY
    send_profile = email_eng.get_send_profile()

    for i, lead in enumerate(leads):
        if sm.stop_event.is_set():
            sm.push_log("🛑 Stopped.")
            break

        sm.push_log(f"  🤖 AI email {i+1}/{len(leads)}: {lead.get('app_name', '')}")
        subject, body = email_eng.ai_rewrite_email(lead, base_subject, base_body)

        ok = _send_email(lead, subject, body)
        if ok:
            sm.inc_sent()
            sheets.sheet_mark_sent(
                lead.get("app_id", ""), lead.get("email", ""), lead.get("app_name", "")
            )
        else:
            sm.inc_failed()

        if i < len(leads) - 1 and not sm.stop_event.is_set():
            email_eng.humanized_delay(
                sm.stop_event, profile=send_profile,
                lead_idx=i, total=len(leads)
            )

    sm.push_log(f"✅ Pending done. Sent: {sm.get_state()['emails_sent']}")
    sm.upd(running=False, phase="done")


def _build_html_email(body_text: str, sender_email: str, sender_name: str,
                       tracking_url: str = "") -> str:
    """Build styled HTML email with gold accent, unsubscribe footer, tracking pixel."""
    escaped  = body_text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    rows     = []
    for line in escaped.split("\n"):
        t = line.strip()
        if not t:
            rows.append('<tr><td style="height:10px;font-size:0">&nbsp;</td></tr>')
        else:
            rows.append(f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;'
                        f'line-height:1.75;color:#2c2c2c;padding:0 0 2px 0">{t}</td></tr>')
    html_rows    = "\n".join(rows)
    display_name = sender_name or sender_email
    unsub = (f"mailto:{sender_email}?subject=Unsubscribe"
             f"&body=Please%20remove%20me%20from%20your%20mailing%20list.%20Thank%20you.")
    pixel = (f'<img src="{tracking_url}" width="1" height="1" alt="" '
             f'style="display:none" border="0">') if tracking_url else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f0f0">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f0f0;padding:32px 16px">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" border="0"
 style="max-width:600px;width:100%;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">
<tr><td height="5" style="background:linear-gradient(90deg,#c9a84c,#e8c76a,#c9a84c);font-size:0">&nbsp;</td></tr>
<tr><td style="padding:36px 44px 28px">
<table width="100%" cellpadding="0" cellspacing="0" border="0">{html_rows}</table>
</td></tr>
<tr><td style="padding:0 44px"><div style="border-top:1px solid #ececec"></div></td></tr>
<tr><td style="padding:22px 44px 30px;text-align:center">
<p style="font-family:Arial,sans-serif;font-size:11px;color:#aaa;margin:0 0 14px;line-height:1.6">
You received this message because your app was found on Google Play Store.<br>
To opt out from <strong>{display_name}</strong>, click below.</p>
<a href="{unsub}" style="display:inline-block;padding:9px 28px;background:#f7f7f7;color:#777;
text-decoration:none;border-radius:6px;border:1px solid #ddd;
font-family:Arial,sans-serif;font-size:12px;font-weight:500">Unsubscribe</a>
</td></tr>
<tr><td height="4" style="background:linear-gradient(90deg,#c9a84c,#e8c76a,#c9a84c);font-size:0">&nbsp;</td></tr>
</table></td></tr></table>
{pixel}
</body></html>"""


def _send_via_smtp(lead: dict, subject: str, body: str,
                   html: str, gmail_address: str, app_password: str,
                   sender_name: str) -> bool:
    """Send via Gmail SMTP + App Password — no Apps Script, no daily limits."""
    from_addr = f"{sender_name} <{gmail_address}>" if sender_name else gmail_address
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = lead["email"]
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as server:
            server.ehlo()
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, lead["email"], msg.as_string())
        sm.push_log(f"  📧 [SMTP] Sent → {lead['email']} ({lead.get('app_name', '')})")
        return True
    except smtplib.SMTPAuthenticationError:
        sm.push_log("  ❌ SMTP Auth failed — check Gmail address + App Password")
        return False
    except Exception as e:
        sm.push_log(f"  ❌ SMTP error: {e}")
        return False


def _send_via_apps_script(lead: dict, subject: str, body: str,
                           html: str, sender_email: str,
                           sender_name: str, tracking_url: str) -> bool:
    """Send via Apps Script URL rotation."""
    import requests as _req
    urls = _parse_email_urls()
    if not urls:
        sm.push_log("  ❌ No Email Script URLs configured")
        return False
    payload = {
        "action":             "send_email",
        "to":                 lead["email"],
        "subject":            subject,
        "body":               body,
        "sender_name":        sender_name,
        "from_email":         sender_email,
        "tracking_pixel_url": tracking_url,
    }
    tried = set()
    for _ in range(len(urls)):
        url = _next_email_url()
        if not url or url in tried:
            continue
        tried.add(url)
        try:
            r      = _req.post(url, json=payload, timeout=30)
            result = r.json() if r.text else {}
            if result.get("status") == "ok":
                _mark_email_ok(url)
                sm.push_log(f"  📧 [GAS] Sent → {lead['email']} ({lead.get('app_name', '')})")
                return True
            _mark_email_fail(url)
            sm.push_log(f"  ⚠️  GAS failed ({url[:40]}…): {result.get('msg','?')}")
        except Exception as e:
            _mark_email_fail(url)
            sm.push_log(f"  ⚠️  GAS error ({url[:40]}…): {e}")
    sm.push_log(f"  ❌ All email methods failed for {lead['email']}")
    return False


def _send_email(lead: dict, subject: str, body: str) -> bool:
    """Route email through SMTP (if App Password set) or Apps Script URLs.
    Performs real-time email verification before sending to reduce bounces.
    """
    import app_verify as _ev
    if not lead.get("email"):
        sm.push_log("  ❌ Missing email address")
        return False

    # ── Pre-send email verification ────────────────────────────────────────
    email = lead["email"]
    valid, confidence, reason = _ev.verify_email(email)
    if not valid:
        sm.push_log(f"  ❌ Skip — email verification failed ({reason}): {email}")
        return False
    if confidence < 0.3:
        sm.push_log(f"  ❌ Skip — email confidence too low ({confidence:.2f}, {reason}): {email}")
        return False
    if confidence < 0.6:
        sm.push_log(f"  ⚠️  Low confidence email ({confidence:.2f}) — sending anyway: {email}")

    sender_name   = get_cfg("SENDER_NAME",   "")
    gmail_address = get_cfg("GMAIL_ADDRESS", "")
    app_password  = get_cfg("APP_PASSWORD",  "")

    # Build tracking pixel URL using APP_URL env (e.g. https://yourapp.onrender.com)
    import uuid as _uuid
    tracking_token = _uuid.uuid4().hex
    app_base_url   = get_cfg("APP_URL", "").rstrip("/")
    tracking_url   = f"{app_base_url}/track/open/{tracking_token}" if app_base_url else ""

    # Build HTML email with tracking pixel (works when APP_URL is set in Settings/env)
    html = _build_html_email(body, gmail_address, sender_name, tracking_url)

    # ── Gmail SMTP + App Password (only sending method) ──────────────────────
    if gmail_address and app_password:
        return _send_via_smtp(lead, subject, body, html,
                              gmail_address, app_password, sender_name)

    sm.push_log("  ❌ No Gmail credentials configured. Go to Settings → Connect Gmail.")
    return False



# ─────────────────────────────────────────────────────────────────────────────
# Persistent server-side config (cross-device settings)
# ─────────────────────────────────────────────────────────────────────────────
_CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playload_config.json")
_server_cfg:  dict = {}
_cfg_lock     = threading.Lock()

def _load_cfg_from_disk() -> dict:
    """Read saved config from disk into memory (called once at startup)."""
    global _server_cfg
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with _cfg_lock:
                _server_cfg = data
            log.info(f"Config loaded from disk ({len(data)} keys)")
            return data
    except Exception as e:
        log.warning(f"Config load error: {e}")
    return {}

def _save_cfg_to_disk(data: dict) -> None:
    """Write config to disk and update in-memory cache."""
    global _server_cfg
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        with _cfg_lock:
            _server_cfg = data
        log.info("Config saved to disk")
    except Exception as e:
        log.error(f"Config save error: {e}")

# Load on startup
_load_cfg_from_disk()

# ─────────────────────────────────────────────────────────────────────────────
# Email open tracking (pixel-based)
# ─────────────────────────────────────────────────────────────────────────────
_TRACKING_PIXEL = bytes([
    0x47,0x49,0x46,0x38,0x39,0x61,0x01,0x00,0x01,0x00,0x80,0x00,0x00,
    0xff,0xff,0xff,0x00,0x00,0x00,0x21,0xf9,0x04,0x00,0x00,0x00,0x00,0x00,
    0x2c,0x00,0x00,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0x02,0x02,0x44,0x01,0x00,0x3b,
])
_opens:      list = []
_opens_lock  = threading.Lock()
_OPENS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_opens.json")

def _load_opens() -> None:
    global _opens
    try:
        if os.path.exists(_OPENS_FILE):
            with open(_OPENS_FILE) as f:
                _opens = json.load(f)
            log.info(f"Loaded {len(_opens)} email opens from disk")
    except Exception as e:
        log.warning(f"Opens load error: {e}")

def _save_opens() -> None:
    try:
        with _opens_lock:
            data = list(_opens)
        with open(_OPENS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Opens save error: {e}")

def _record_open(token: str) -> None:
    record = {"token": token, "opened_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _opens_lock:
        _opens.append(record)
    _save_opens()

_load_opens()   # load persisted opens from disk on startup

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@application.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


@application.route("/api/start", methods=["POST"])
def api_start():
    data    = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400

    state = sm.get_state()
    if state["running"]:
        return jsonify({"error": "Already running"}), 409

    # Build run-time config from request payload + env fallbacks
    rc = {
        "GROQ_API_KEY":        data.get("groq_key")          or os.environ.get("GROQ_API_KEY", ""),
        "APPS_SCRIPT_WEB_URL":  data.get("sheet_url")          or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "SENDER_NAME":         data.get("sender_name")       or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":      data.get("sender_company")    or os.environ.get("SENDER_COMPANY", ""),
        "GMAIL_ADDRESS":       data.get("gmail_address")      or os.environ.get("GMAIL_ADDRESS", ""),
        "APP_PASSWORD":        data.get("app_password")        or os.environ.get("APP_PASSWORD", ""),
        "EMAIL_SUBJECT":       data.get("email_subject")     or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":          data.get("email_body")        or os.environ.get("EMAIL_BODY", ""),
        "SPAM_WORDS":          data.get("spam_words")        or os.environ.get("SPAM_WORDS", ""),
        "SEND_PROFILE":        data.get("send_profile")      or os.environ.get("SEND_PROFILE", "normal"),
    }
    set_run_cfg(rc)

    target = int(data.get("target") or os.environ.get("TARGET_LEADS", 300))
    hunter = data.get("hunter") or {}

    threading.Thread(
        target=run_automation, args=(keyword, target, hunter), daemon=True
    ).start()
    return jsonify({"ok": True, "keyword": keyword, "target": target})


@application.route("/api/stop", methods=["POST"])
def api_stop():
    sm.stop_event.set()
    sm.push_log("🛑 Stop requested.")
    return jsonify({"ok": True})


@application.route("/api/status")
def api_status():
    return jsonify(sm.get_state())


@application.route("/api/clear", methods=["POST"])
def api_clear():
    state = sm.get_state()
    if state["running"]:
        return jsonify({"error": "Cannot clear while running"}), 409
    sm.reset_state(keep_crm=True)
    sm.clear_dedup()
    sm.clear_sheet_memory()
    log.info("State cleared.")
    return jsonify({"ok": True})



@application.route("/track/open/<token>")
def track_open(token: str):
    """Serve 1x1 tracking pixel and record email open."""
    from flask import make_response
    _record_open(token)
    resp = make_response(_TRACKING_PIXEL)
    resp.headers["Content-Type"]  = "image/gif"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    return resp


@application.route("/api/opens")
def api_opens():
    """Return email open records, optionally filtered by date range."""
    start_dt = request.args.get("start", "")
    end_dt   = request.args.get("end",   "")
    with _opens_lock:
        data = list(_opens)
    if start_dt:
        data = [o for o in data if o.get("opened_at", "") >= start_dt]
    if end_dt:
        data = [o for o in data if o.get("opened_at", "") <= end_dt + " 23:59:59"]
    return jsonify({"ok": True, "opens": data, "total": len(data)})


@application.route("/api/script/<script_name>")
def api_get_script(script_name: str):
    """Serve Apps Script (.gs) file content for the dashboard code viewer."""
    allowed = {"sheet": "Code.gs", "email": "EmailSender.gs"}
    filename = allowed.get(script_name)
    if not filename:
        return jsonify({"error": "Unknown script name"}), 404
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        content_gs = open(path, encoding="utf-8").read()
        return jsonify({"ok": True, "content": content_gs, "filename": filename})
    except FileNotFoundError:
        return jsonify({"error": f"{filename} not found on server"}), 404


@application.route("/api/ping", methods=["GET", "POST"])
def api_ping():
    return jsonify({"ok": True, "ts": time.time()})


@application.route("/api/send_pending", methods=["POST"])
def api_send_pending():
    state = sm.get_state()
    if state["running"]:
        return jsonify({"error": "Automation is running"}), 409

    data  = request.get_json(silent=True) or {}
    leads = data.get("leads") or []
    if not leads:
        return jsonify({"error": "No leads provided"}), 400

    rc = {
        "GROQ_API_KEY":        data.get("groq_key")         or os.environ.get("GROQ_API_KEY", ""),
        "APPS_SCRIPT_WEB_URL":  data.get("sheet_url")          or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "SENDER_NAME":         data.get("sender_name")      or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":      data.get("sender_company")   or os.environ.get("SENDER_COMPANY", ""),
        "GMAIL_ADDRESS":       data.get("gmail_address")     or os.environ.get("GMAIL_ADDRESS", ""),
        "APP_PASSWORD":        data.get("app_password")       or os.environ.get("APP_PASSWORD", ""),
        "EMAIL_SUBJECT":       data.get("email_subject")    or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":          data.get("email_body")       or os.environ.get("EMAIL_BODY", ""),
        "SPAM_WORDS":          data.get("spam_words")       or os.environ.get("SPAM_WORDS", ""),
        "SEND_PROFILE":        data.get("send_profile")     or os.environ.get("SEND_PROFILE", "normal"),
    }
    set_run_cfg(rc)

    threading.Thread(target=run_send_pending, args=(leads,), daemon=True).start()
    return jsonify({"ok": True, "count": len(leads)})


@application.route("/api/spam_test", methods=["POST"])
def api_spam_test():
    """Legacy endpoint — delegates to smtp_test for actual SMTP delivery."""
    return api_smtp_test()


@application.route("/api/smtp_test", methods=["POST"])
def api_smtp_test():
    """Send a real test email via Gmail SMTP and return spam analysis."""
    data = request.get_json(silent=True) or {}
    test_to       = (data.get("test_email") or "").strip()
    gmail_address = (data.get("gmail_address") or os.environ.get("GMAIL_ADDRESS", "")).strip()
    app_password  = (data.get("app_password")  or os.environ.get("APP_PASSWORD",  "")).strip()
    sender_name   = (data.get("sender_name")   or os.environ.get("SENDER_NAME",   "")).strip()

    if not test_to:
        return jsonify({"error": "test_email required"}), 400
    if not gmail_address:
        return jsonify({"error": "Gmail address is not configured. Please connect your Gmail account first."}), 400
    if not app_password:
        return jsonify({"error": "Gmail App Password is not configured. Please connect your Gmail account first."}), 400

    rc = {
        "GROQ_API_KEY":   data.get("groq_key")      or os.environ.get("GROQ_API_KEY", ""),
        "SENDER_NAME":    sender_name,
        "SENDER_COMPANY": data.get("sender_company") or os.environ.get("SENDER_COMPANY", ""),
        "GMAIL_ADDRESS":  gmail_address,
        "APP_PASSWORD":   app_password,
        "EMAIL_SUBJECT":  data.get("email_subject")  or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":     data.get("email_body")     or os.environ.get("EMAIL_BODY", ""),
        "SPAM_WORDS":     data.get("spam_words")     or os.environ.get("SPAM_WORDS", ""),
    }
    set_run_cfg(rc)

    sample = {
        "app_id":    "com.example.testapp",
        "app_name":  "FinanceTrack Pro",
        "developer": "Alex Dev",
        "category":  "Finance",
        "installs":  1200,
        "score":     2.1,
        "email":     test_to,
        "url":       "https://play.google.com/store/apps/details?id=com.example.testapp",
    }

    base_subject = get_cfg("EMAIL_SUBJECT") or email_eng.DEFAULT_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or email_eng.DEFAULT_BODY
    subject, body = email_eng.ai_rewrite_email(sample, base_subject, base_body)
    check = email_eng.spam_score(subject, body)

    html = _build_html_email(body, gmail_address, sender_name, "")
    from_addr = f"{sender_name} <{gmail_address}>" if sender_name else gmail_address
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "[TEST] " + subject
    msg["From"]    = from_addr
    msg["To"]      = test_to
    msg["X-Mailer"] = "PlayLeadBot-Test"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as server:
            server.ehlo()
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, test_to, msg.as_string())
        log.info(f"Test email sent via SMTP_SSL to {test_to}")
        return jsonify({
            "ok": True,
            "msg": f"Test email delivered to {test_to} via Gmail SMTP",
            "subject": subject,
            "body": body,
            "spam_check": check
        })
    except smtplib.SMTPAuthenticationError:
        return jsonify({"error": "Gmail authentication failed. Check your Gmail address and App Password. Ensure 2-Step Verification is ON and you are using a valid App Password (16 characters)."}), 401
    except smtplib.SMTPRecipientsRefused:
        return jsonify({"error": f"Recipient address refused: {test_to}"}), 400
    except smtplib.SMTPException as e:
        return jsonify({"error": f"SMTP error: {str(e)}"}), 500
    except Exception as e:
        log.error(f"Unexpected error during test email: {e}")
        return jsonify({"error": f"Failed to send: {str(e)}"}), 500


@application.route("/api/smtp_connect", methods=["POST"])
def api_smtp_connect():
    """Validate Gmail SMTP credentials by opening a live connection (no email sent)."""
    data          = request.get_json(silent=True) or {}
    gmail_address = (data.get("gmail_address") or "").strip()
    app_password  = (data.get("app_password")  or "").strip()
    sender_name   = (data.get("sender_name")   or "").strip()

    if not gmail_address:
        return jsonify({"error": "Gmail address is required"}), 400
    if not app_password:
        return jsonify({"error": "App Password is required"}), 400
    if not sender_name:
        return jsonify({"error": "Sender name is required"}), 400

    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=20) as server:
            server.ehlo()
            server.login(gmail_address, app_password)
        # Persist verified credentials
        existing = dict(_server_cfg)
        existing["gmailAddress"] = gmail_address
        existing["appPassword"]  = app_password
        existing["senderName"]   = sender_name
        _save_cfg_to_disk(existing)
        set_run_cfg({
            "GMAIL_ADDRESS": gmail_address,
            "APP_PASSWORD":  app_password,
            "SENDER_NAME":   sender_name,
        })
        log.info(f"SMTP credentials verified for {gmail_address}")
        return jsonify({"ok": True, "msg": f"Connected as {gmail_address}"})
    except smtplib.SMTPAuthenticationError:
        return jsonify({"error": "Authentication failed. Check your Gmail address and App Password. Make sure 2-Step Verification is ON and use a 16-character App Password from Google Account → Security → App Passwords."}), 401
    except smtplib.SMTPConnectError as e:
        return jsonify({"error": f"Cannot connect to Gmail SMTP (port 465): {e}"}), 503
    except OSError as e:
        return jsonify({"error": f"Network error connecting to Gmail: {e}. Please check server network access."}), 503
    except Exception as e:
        return jsonify({"error": f"Connection error: {str(e)}"}), 500


@application.route("/api/sheet_pending", methods=["POST"])
def api_sheet_pending():
    data      = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400
    rc = {"APPS_SCRIPT_WEB_URL": sheet_url}
    set_run_cfg(rc)
    try:
        leads = sheets.sheet_fetch_pending()
        return jsonify({"ok": True, "count": len(leads), "leads": leads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@application.route("/api/sheet_memory_status")
def api_sheet_memory_status():
    return jsonify(sm.sheet_memory_stats())


@application.route("/api/url_pool_status")
def api_url_pool_status():
    return jsonify(sm.get_state() | {"url_pool": email_pool_status()})



@application.route("/api/save_config", methods=["POST"])
def api_save_config():
    """Save dashboard settings to disk (cross-device persistence)."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No data"}), 400
    _save_cfg_to_disk(data)
    return jsonify({"ok": True})


@application.route("/api/load_config")
def api_load_config():
    """Return saved dashboard settings to any device that opens the dashboard."""
    with _cfg_lock:
        config = dict(_server_cfg)
    return jsonify({"ok": True, "config": config})


@application.route("/api/spam_check", methods=["POST"])
def api_spam_check():
    """Inline spam score check for the editor preview."""
    data    = request.get_json(silent=True) or {}
    subject = data.get("subject", "")
    body    = data.get("body", "")
    result  = email_eng.spam_score(subject, body)
    return jsonify(result)


@application.route("/api/keyword_score", methods=["POST"])
def api_keyword_score():
    """Score a keyword for intent quality."""
    data = request.get_json(silent=True) or {}
    kw   = (data.get("keyword") or "").strip()
    if not kw:
        return jsonify({"error": "keyword required"}), 400
    return jsonify(kw_eng.score_keyword_public(kw))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
