# PlayLead Engine v2.0

A production-grade AI-powered lead generation and B2B outreach platform for finding Android app developers on Google Play Store.

---

## What's New in v2.0

| Feature | Status |
|---|---|
| Rebuilt rating extraction with confidence scoring | ✅ |
| Hunter Mode / Normal Mode enforcement (fully separate logic) | ✅ |
| AI keyword intent scoring — rejects low-value categories | ✅ |
| AI email rewriting — unique email per lead | ✅ |
| Spam word filter with auto-replacement | ✅ |
| Live spam score analyser | ✅ |
| Multi-URL Apps Script failover system | ✅ |
| Gmail alias sender support | ✅ |
| Humanized randomized send delays | ✅ |
| Email verification pipeline (syntax + DNS + disposable check) | ✅ |
| Email Overview / CRM analytics dashboard | ✅ |
| Campaign session history | ✅ |
| Keyword intent score display | ✅ |
| Modular architecture — clean separation of concerns | ✅ |

---

## Project Structure

```
PlayLeadEngine/
├── main.py                    # Flask app — all routes
├── dashboard.html             # Full-featured dark-gold UI
├── Code.gs                    # Google Apps Script (Sheets + Gmail)
├── requirements.txt
├── runtime.txt
├── Procfile
├── render.yaml
├── .env.example
└── modules/
    ├── config.py              # Centralised config management
    ├── state_manager.py       # Thread-safe shared state + dedup
    ├── scraper.py             # Play Store scraper with confidence scoring
    ├── keyword_engine.py      # AI keyword generation with intent scoring
    ├── email_engine.py        # AI rewriting + spam filter + delays
    ├── email_verify.py        # Email verification pipeline
    └── sheet_manager.py       # Multi-URL Apps Script integration
```

---

## Quick Start (Local)

```bash
# 1. Clone / unzip
cd PlayLeadEngine

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment
cp .env.example .env
# Edit .env with your actual keys

# 5. Run
python main.py
# Dashboard → http://localhost:5000
```

---

## Deploy to Render (Recommended)

1. Push project to a GitHub repo
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your repo
4. Render auto-detects `render.yaml` and configures the service
5. Add environment variables in the Render dashboard (Environment tab)
6. Deploy

**Required env vars on Render:**
- `GROQ_API_KEY`
- `APPS_SCRIPT_WEB_URLS`
- `EMAIL_SCRIPT_URL`
- `SENDER_NAME`
- `SENDER_COMPANY`

---

## Google Apps Script Setup

### Step 1 — Create the Sheet Script (database)

1. Open your Google Sheet
2. Extensions → Apps Script
3. Delete all existing code
4. Paste the entire contents of `Code.gs`
5. Save (Ctrl+S)
6. Click **Deploy → New Deployment**
   - Type: **Web App**
   - Execute as: **Me**
   - Who has access: **Anyone**
7. Click **Deploy** → authorize when prompted
8. Copy the **Web App URL**
9. Paste it into PlayLead Settings → **Apps Script Web URLs**

### Step 2 — Multi-URL Quota Failover

To avoid hitting Google's daily Apps Script quota:

1. Create 2–3 separate Google Sheets, each with its own `Code.gs` deployment
2. Paste all Web App URLs into the **Apps Script Web URLs** field (one per line)
3. PlayLead automatically rotates to the next URL if one hits quota

### Step 3 — Email Sender Script

The email sender can use the same `Code.gs` deployment or a separate one.
The `send_email` action calls `GmailApp.sendEmail()` from your Gmail account.

> **Note:** Gmail has a daily send limit of 100–500 emails/day depending on account type. Google Workspace accounts have higher limits.

### Step 4 — Gmail Alias (optional)

To send from a custom address (e.g. `outreach@yourdomain.com`):

1. In Gmail → Settings (gear icon) → See all settings
2. Go to **Accounts and Import** → **Send mail as**
3. Add your alias email and verify it
4. In PlayLead Settings → enter that email in **Sender Alias**

---

## Modes Explained

### Normal Mode
- Targets **brand new apps** with **zero ratings and zero reviews**
- Goal: Reach developers who just launched and need their first reviews
- Skips any app that already has ratings

### Hunter Mode
- Targets apps that **already have ratings but low scores**
- Configurable: max installs, max rating score (e.g. only apps with ≤ 2.5★)
- Goal: Offer reputation recovery to developers struggling with bad reviews

---

## Rating Confidence System

The scraper extracts rating data from multiple fields and assigns a confidence score (0–1):

| Confidence | Meaning |
|---|---|
| 0.9–1.0 | Multiple signals confirm the result (score + ratings + reviews + histogram) |
| 0.7–0.8 | Two signals agree |
| 0.5–0.6 | Single signal, possibly ambiguous |
| 0.0 | Confirmed no ratings (all signals = zero/null) |

Leads with confidence below 0.5 are filtered out in Hunter Mode.

---

## AI Keyword Intent Scoring

Keywords are scored 0–1 for commercial intent before being used:

- **High value (0.7+):** finance, trading, crypto, insurance, dating, VPN, telemedicine, legal, education, marketplaces, delivery
- **Medium value (0.3–0.7):** productivity tools, fitness, travel, social apps
- **Rejected (<0.3):** calculator, flashlight, wallpaper, QR scanner, file manager, alarm clock, unit converter

The AI keyword generator is prompted to only produce high-value categories.

---

## Email Deliverability Tips

1. **SPF / DKIM / DMARC** — Set up all three DNS records for your sending domain
2. **Warm up** — Start with 20 emails/day for the first 2 weeks; increase gradually
3. **Send profile** — Use `conservative` (90–180s delays) for new accounts
4. **Spam words** — Add your own in Settings → Spam Word Filter
5. **Subject lines** — Keep under 50 characters; avoid ALL CAPS and excessive punctuation
6. **Plain text** — The template uses plain text by default (best for deliverability)
7. **Unsubscribe** — Add an unsubscribe note to comply with CAN-SPAM / GDPR

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/start` | POST | Start automation with config payload |
| `/api/stop` | POST | Stop running automation |
| `/api/status` | GET | Get current state + leads + logs |
| `/api/clear` | POST | Clear session state |
| `/api/send_pending` | POST | Send emails to provided leads list |
| `/api/sheet_pending` | POST | Fetch pending leads from sheet |
| `/api/spam_test` | POST | Send test email + get spam score |
| `/api/spam_check` | POST | Check subject+body spam score |
| `/api/keyword_score` | POST | Score a keyword for intent |
| `/api/url_pool_status` | GET | Get Apps Script URL pool health |
| `/api/ping` | GET | Keep-alive / health check |

---

## Migration from v1

No database migration needed — the sheet schema is backwards compatible.

Changes to your Apps Script (`Code.gs`):
- Replace your existing `Code.gs` entirely with the new version
- Redeploy as a new deployment (or update existing)
- The new script adds `get_all`, `get_pending`, `send_email` actions

Environment variable changes:
- `APPS_SCRIPT_WEB_URL` still works (single URL)
- New: `APPS_SCRIPT_WEB_URLS` supports multiple URLs (multi-line)
- New: `SENDER_ALIAS` for Gmail alias support
- New: `SPAM_WORDS` for custom spam word filter
- New: `SEND_PROFILE` for delay configuration

---

## Troubleshooting

**Leads = 0 for a keyword**
- Try a more specific keyword (2–4 words)
- Use a high-intent category (finance, trading, crypto, VPN, dating)
- Check the live log for filter reasons

**Rating shows 0 / confidence is low**
- Some apps genuinely have no ratings (correct for Normal Mode)
- The confidence system filters ambiguous cases in Hunter Mode

**Emails not sending**
- Verify `EMAIL_SCRIPT_URL` is correct and deployed
- Check the Apps Script execution log in Google Apps Script editor
- Verify Gmail daily send limit not exceeded

**Apps Script quota exceeded**
- Add more Web App URLs in Settings → Apps Script Web URLs
- System auto-rotates every 5 minutes per failed URL

---

## License

Private use only. Not for redistribution.
