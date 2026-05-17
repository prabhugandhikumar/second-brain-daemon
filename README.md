# Second Brain Daemon

The Python service that powers Prabhu's Second Brain. Deploys as a **single Cloud Run service** named `secondbrain` in the `tabp-second-brain` GCP project (Mumbai region).

## What it does

| Endpoint | Triggered by | What it does |
|---|---|---|
| `POST /webhook/telegram` | Telegram (set as webhook URL for @PrabhuBrainBot) | Receives screenshots, voice notes, text, button replies. Classifies and routes. |
| `POST /cron/poll-drive` | Cloud Scheduler every 15 min | Lists new files in Drive Brain Inbox, classifies, writes to Notion. |
| `POST /cron/morning-briefing` | Cloud Scheduler at 5:00 AM IST daily | Pulls open commitments from Notion, sends email digest, sends Telegram briefing with action buttons. |
| `GET /` | Browser (briefing.tabp.co.in) | Login-protected web dashboard. |
| `POST /login` | Browser | Form login with bcrypt-hashed passwords from Secret Manager. |
| `GET /healthz` | Cloud Run health check | Returns 200 OK if app is alive. |

## Architecture

```
┌────────────────────────────────────────────────────────┐
│  Cloud Run service: "secondbrain" (asia-south1)        │
│                                                         │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐        │
│  │  Telegram  │  │   Drive    │  │  Briefing  │        │
│  │  webhook   │  │   poller   │  │   cron     │        │
│  └────┬───────┘  └─────┬──────┘  └─────┬──────┘        │
│       │                │               │                │
│       └────────┬───────┴───────┬───────┘                │
│                │               │                        │
│         ┌──────▼──────┐ ┌──────▼──────┐                │
│         │  Classify   │ │   Notion    │                │
│         │  (Sonnet)   │ │   Writer    │                │
│         └─────────────┘ └─────────────┘                │
└────────────────────────────────────────────────────────┘
        ▲                                ▲
        │                                │
    Telegram API                    Notion API
    Whisper API                     Gmail API
    Drive API                       Outlook API
```

All secrets live in **GCP Secret Manager**: `telegram-bot-token`, `anthropic-api-key`, `notion-api-token`, `gmail-oauth-refresh-token`, `dashboard-passwords` (bcrypt YAML), `whisper-api-key`.

## Directory layout

```
daemon/
├── README.md           — this file
├── requirements.txt    — pip dependencies
├── Dockerfile          — Cloud Run-compatible container build
├── deploy.sh           — single-command deploy to Cloud Run
├── .env.example        — template; never commit a real .env
├── main.py             — FastAPI app, route registration
├── handlers/
│   ├── telegram.py     — webhook handler: text / voice / photo / callback
│   ├── drive_poll.py   — Drive Brain Inbox poll + classify
│   └── briefing.py     — morning digest builder + delivery
├── lib/
│   ├── secrets.py      — GCP Secret Manager access
│   ├── classify.py     — Sonnet vision classification of screenshots
│   ├── notion_writer.py — idempotent Notion DB writes
│   ├── transcribe.py   — voice note → Whisper text
│   ├── drive.py        — Google Drive API client
│   ├── gmail.py        — Gmail send + read
│   ├── outlook.py      — Microsoft Graph send + read
│   └── prompts.py      — Anthropic prompt templates
└── web/
    ├── dashboard.py    — Flask-style routes for the live web UI
    ├── auth.py         — form login + bcrypt password check
    └── templates/      — Jinja2 templates for HTML pages
```

## Deployment (overview, full steps in `deploy.sh`)

```bash
# 1. Authenticate with the project
gcloud auth login
gcloud config set project tabp-second-brain
gcloud config set run/region asia-south1

# 2. Push secrets (run once, then update via `gcloud secrets versions add`)
echo -n "8582635772:..." | gcloud secrets create telegram-bot-token --data-file=-
echo -n "sk-ant-..." | gcloud secrets create anthropic-api-key --data-file=-
# ... etc

# 3. Build + deploy
gcloud run deploy secondbrain \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --service-account secondbrain@tabp-second-brain.iam.gserviceaccount.com

# 4. Register the Cloud Run URL as Telegram webhook
curl -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  -d "url=https://secondbrain-xxxx.run.app/webhook/telegram"

# 5. Schedule the cron jobs
gcloud scheduler jobs create http poll-drive \
  --schedule "*/15 * * * *" \
  --uri "https://secondbrain-xxxx.run.app/cron/poll-drive" \
  --http-method POST

gcloud scheduler jobs create http morning-briefing \
  --schedule "0 5 * * *" \
  --time-zone "Asia/Kolkata" \
  --uri "https://secondbrain-xxxx.run.app/cron/morning-briefing" \
  --http-method POST

# 6. Map the custom domain
gcloud beta run domain-mappings create \
  --service secondbrain \
  --domain briefing.tabp.co.in \
  --region asia-south1
# Then add the CNAME at GoDaddy as instructed by the output.
```

`deploy.sh` wraps this so it's one command.

## Local development

```bash
cp .env.example .env  # fill in dev values
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

Test webhooks locally with [ngrok](https://ngrok.com) pointed at `localhost:8080`.

## What this daemon does NOT do

- It does not read your phone storage directly — Autosync handles screenshot upload to Drive.
- It does not bypass WhatsApp — incoming WhatsApp commitments arrive via screenshots you take.
- It does not auto-send messages on your behalf to anyone (Telegram, email, WhatsApp, etc.) without you tapping a button or replying first.

## Status

Built 2026-05-11 by Prabhu + Claude in Cowork mode. See `MEMORY.md` in the parent Second Brain folder for system context and the Notion workspace for active commitments.
