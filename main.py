"""
main.py — PlayLead Engine v2.1
Flask entry point. All business logic lives in modules/.
"""

import os


import json
import time
import threading
import logging

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


def _send_email(lead: dict, subject: str, body: str) -> bool:
    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url or not lead.get("email"):
        sm.push_log("  ❌ EMAIL_SCRIPT_URL not set or missing email")
        return False

    # Determine sender alias
    alias = get_cfg("SENDER_ALIAS", "")

    payload = {
        "action":      "send_email",
        "to":          lead["email"],
        "subject":     subject,
        "body":        body,
        "sender_name": get_cfg("SENDER_NAME", ""),
    }
    if alias:
        payload["from_alias"] = alias

    try:
        import requests as _req
        r = _req.post(url, json=payload, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            sm.push_log(f"  📧 Sent → {lead['email']} ({lead.get('app_name', '')})")
            return True
        sm.push_log(f"  ❌ Email failed: {result.get('msg', '?')}")
        return False
    except Exception as e:
        sm.push_log(f"  ❌ Email error: {e}")
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
        "APPS_SCRIPT_WEB_URLS": data.get("sheet_urls")        or data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URLS", "") or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "APPS_SCRIPT_WEB_URL": data.get("sheet_url")         or os.environ.get("APPS_SCRIPT_WEB_URL", ""),
        "EMAIL_SCRIPT_URL":    data.get("email_script_url")  or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":         data.get("sender_name")       or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":      data.get("sender_company")    or os.environ.get("SENDER_COMPANY", ""),
        "SENDER_ALIAS":        data.get("sender_alias")      or os.environ.get("SENDER_ALIAS", ""),
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
        "APPS_SCRIPT_WEB_URLS": data.get("sheet_urls")       or data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URLS", ""),
        "EMAIL_SCRIPT_URL":    data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":         data.get("sender_name")      or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":      data.get("sender_company")   or os.environ.get("SENDER_COMPANY", ""),
        "SENDER_ALIAS":        data.get("sender_alias")     or os.environ.get("SENDER_ALIAS", ""),
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
    data = request.get_json(silent=True) or {}
    test_to = (data.get("test_email") or "").strip()
    if not test_to:
        return jsonify({"error": "test_email required"}), 400

    rc = {
        "GROQ_API_KEY":     data.get("groq_key")        or os.environ.get("GROQ_API_KEY", ""),
        "EMAIL_SCRIPT_URL": data.get("email_script_url") or os.environ.get("EMAIL_SCRIPT_URL", ""),
        "SENDER_NAME":      data.get("sender_name")     or os.environ.get("SENDER_NAME", ""),
        "SENDER_COMPANY":   data.get("sender_company")  or os.environ.get("SENDER_COMPANY", ""),
        "SENDER_ALIAS":     data.get("sender_alias")    or os.environ.get("SENDER_ALIAS", ""),
        "EMAIL_SUBJECT":    data.get("email_subject")   or os.environ.get("EMAIL_SUBJECT", ""),
        "EMAIL_BODY":       data.get("email_body")      or os.environ.get("EMAIL_BODY", ""),
        "SPAM_WORDS":       data.get("spam_words")      or os.environ.get("SPAM_WORDS", ""),
    }
    set_run_cfg(rc)

    sample = {
        "app_name":  data.get("sample_app_name", "FinanceTrack Pro"),
        "developer": data.get("sample_developer", "Alex Dev"),
        "category":  "Finance",
        "installs":  1200,
        "score":     data.get("sample_score", 2.1),
        "email":     test_to,
        "url":       "https://play.google.com/store/apps/details?id=com.example.finance",
    }

    base_subject = get_cfg("EMAIL_SUBJECT") or email_eng.DEFAULT_SUBJECT
    base_body    = get_cfg("EMAIL_BODY")    or email_eng.DEFAULT_BODY
    subject, body = email_eng.ai_rewrite_email(sample, base_subject, base_body)
    check = email_eng.spam_score(subject, body)

    url = get_cfg("EMAIL_SCRIPT_URL")
    if not url:
        return jsonify({
            "ok": True, "skipped_send": True,
            "subject": subject, "body": body, "spam_check": check,
            "msg": "No EMAIL_SCRIPT_URL — preview only"
        })

    try:
        import requests as _req
        payload = {"to": test_to, "subject": subject, "body": body}
        alias = get_cfg("SENDER_ALIAS", "")
        if alias:
            payload["from_alias"] = alias
        r = _req.post(url, json=payload, timeout=30)
        result = r.json() if r.text else {}
        if result.get("status") == "ok":
            return jsonify({
                "ok": True, "msg": f"Test sent to {test_to}",
                "subject": subject, "body": body, "spam_check": check
            })
        return jsonify({"error": result.get("msg", "Failed")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@application.route("/api/sheet_pending", methods=["POST"])
def api_sheet_pending():
    data      = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_urls") or data.get("sheet_url") or os.environ.get("APPS_SCRIPT_WEB_URLS", "") or os.environ.get("APPS_SCRIPT_WEB_URL", "")
    if not sheet_url:
        return jsonify({"error": "sheet_url not set"}), 400
    rc = {"APPS_SCRIPT_WEB_URLS": sheet_url, "APPS_SCRIPT_WEB_URL": sheet_url}
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
    return jsonify(sm.get_state() | {"url_pool": sheets.url_pool_status()})



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
