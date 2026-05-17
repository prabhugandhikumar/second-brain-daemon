#!/usr/bin/env bash
# Second Brain — one-command deploy to GCP Cloud Run.
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh          # full deploy
#   ./deploy.sh secrets  # just push secrets (skip build + deploy)
#   ./deploy.sh code     # just rebuild + redeploy (skip secrets)
#
# Prereqs:
#   - gcloud CLI installed and authenticated (`gcloud auth login`)
#   - Run from the daemon/ directory

set -euo pipefail

PROJECT_ID="tabp-second-brain"
REGION="asia-south1"
SERVICE_NAME="secondbrain"
SERVICE_ACCOUNT_NAME="secondbrain"

MODE="${1:-all}"

echo "═══════════════════════════════════════════════════════"
echo "  Second Brain Deploy"
echo "  Project:  $PROJECT_ID"
echo "  Region:   $REGION"
echo "  Service:  $SERVICE_NAME"
echo "  Mode:     $MODE"
echo "═══════════════════════════════════════════════════════"

# ─── Pre-flight checks ─────────────────────────────────────
gcloud config set project "$PROJECT_ID" >/dev/null
gcloud config set run/region "$REGION" >/dev/null

# ─── 1. Service account ────────────────────────────────────
SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if [[ "$MODE" == "all" || "$MODE" == "iam" ]]; then
  echo ""
  echo "▸ Ensuring service account $SA_EMAIL exists..."
  gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
      --display-name="Second Brain runtime"

  echo "▸ Granting IAM roles to the service account..."
  # secretmanager.admin (not just secretAccessor) is needed because
  # /cron/refresh-models writes new versions of the gemini-model-chain
  # secret. The other roles are unchanged.
  for role in roles/secretmanager.admin roles/run.invoker roles/logging.logWriter; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:$SA_EMAIL" \
      --role="$role" \
      --condition=None >/dev/null
  done
fi

# ─── 2. Push secrets to Secret Manager ─────────────────────
push_secret () {
  local name="$1"
  local prompt="$2"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    read -r -p "  $prompt (secret '$name' already exists — update? [y/N]) " yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
      read -r -s -p "    new value: " value && echo
      echo -n "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
      echo "    ✓ updated"
    else
      echo "    (keeping existing)"
    fi
  else
    read -r -s -p "  $prompt: " value && echo
    echo -n "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
    echo "    ✓ created"
  fi
}

if [[ "$MODE" == "all" || "$MODE" == "secrets" ]]; then
  echo ""
  echo "▸ Pushing secrets to Secret Manager (press enter to skip)..."
  push_secret "telegram-bot-token"  "Telegram bot token (from @BotFather)"
  push_secret "gemini-api-key"      "Gemini API key (from aistudio.google.com)"
  push_secret "notion-api-token"    "Notion integration token (from notion.so/profile/integrations)"
  push_secret "openai-api-key"      "OpenAI API key (for Whisper voice transcription) — leave blank to skip"
  push_secret "session-secret"      "Session cookie signing secret (any 32+ random chars)"
  push_secret "dashboard-users-yaml" "Dashboard users YAML (e.g. 'prabhu: \$2b\$12\$...')"
fi

# ─── 3. Build + deploy to Cloud Run ────────────────────────
if [[ "$MODE" == "all" || "$MODE" == "code" ]]; then
  echo ""
  echo "▸ Building + deploying $SERVICE_NAME to Cloud Run..."
  gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --region "$REGION" \
    --allow-unauthenticated \
    --service-account "$SA_EMAIL" \
    --memory 1Gi \
    --cpu 1 \
    --max-instances 3 \
    --timeout 300 \
    --set-env-vars "GCP_PROJECT_ID=$PROJECT_ID,DRIVE_BRAIN_INBOX_FOLDER_ID=1-Qh-aklaTB9CNr7QTQgdum_QKdFNOLkL,TELEGRAM_OWNER_CHAT_ID=937808375,NOTION_COMMITMENTS_DS=d42a2708-214e-4f31-8933-422dee8d0a09,NOTION_TASKS_DS=184cc773-5a57-4fe2-848d-499a85adfbeb,NOTION_BRIEFINGS_DS=4f32be90-b07c-41bd-add5-58a55d6e1b1c,NOTION_CONVERSATIONS_DS=348c3ae8-9729-41e5-945f-2da9c241ac19,NOTION_MEETINGS_DS=60e7301c-2a44-4c6e-8b31-c46a1b930770,NOTION_REPORTS_DUE_DS=67fe49c0-9bae-41c7-a1b0-594a42a0c450"
fi

# ─── 4. Get the deployed service URL ───────────────────────
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)')
echo ""
echo "▸ Cloud Run URL: $SERVICE_URL"

# ─── 5. Set Telegram webhook ───────────────────────────────
if [[ "$MODE" == "all" || "$MODE" == "webhook" ]]; then
  echo ""
  echo "▸ Registering Telegram webhook..."
  TOKEN=$(gcloud secrets versions access latest --secret=telegram-bot-token)
  curl -s -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
    -d "url=${SERVICE_URL}/webhook/telegram" \
    -d "drop_pending_updates=true" \
    -d "allowed_updates=[\"message\",\"edited_message\",\"callback_query\"]" \
    | python3 -m json.tool
fi

# ─── 6. Cloud Scheduler jobs ───────────────────────────────
ensure_scheduler () {
  local job_name="$1"
  local schedule="$2"
  local endpoint="$3"
  if gcloud scheduler jobs describe "$job_name" --location "$REGION" >/dev/null 2>&1; then
    echo "  ✓ $job_name already exists — updating"
    gcloud scheduler jobs update http "$job_name" --location "$REGION" \
      --schedule "$schedule" \
      --uri "${SERVICE_URL}${endpoint}" \
      --http-method POST \
      --time-zone "Asia/Kolkata" \
      --oidc-service-account-email "$SA_EMAIL" >/dev/null
  else
    gcloud scheduler jobs create http "$job_name" --location "$REGION" \
      --schedule "$schedule" \
      --uri "${SERVICE_URL}${endpoint}" \
      --http-method POST \
      --time-zone "Asia/Kolkata" \
      --oidc-service-account-email "$SA_EMAIL" >/dev/null
    echo "  ✓ $job_name created"
  fi
}

if [[ "$MODE" == "all" || "$MODE" == "scheduler" ]]; then
  echo ""
  echo "▸ Setting up Cloud Scheduler jobs..."
  ensure_scheduler "poll-drive" "*/15 * * * *" "/cron/poll-drive"
  ensure_scheduler "morning-briefing" "0 5 * * *" "/cron/morning-briefing"
  # Weekly: discover Gemini models, rebuild fallback chain, DM if changed.
  ensure_scheduler "refresh-models" "0 3 * * 0" "/cron/refresh-models"
fi

# ─── Done ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✅ Deploy complete"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  Service URL:  $SERVICE_URL"
echo "  Telegram bot: @PrabhuBrainBot"
echo "  Dashboard:    $SERVICE_URL/   (will become briefing.tabp.co.in after DNS)"
echo ""
echo "Try sending @PrabhuBrainBot a /start message in Telegram to confirm webhook."
echo ""
echo "To map briefing.tabp.co.in:"
echo "  1. gcloud beta run domain-mappings create --service $SERVICE_NAME --domain briefing.tabp.co.in --region $REGION"
echo "  2. The output gives DNS records to add at GoDaddy."
echo "  3. DNS propagation + SSL cert provisioning takes ~30 min."
