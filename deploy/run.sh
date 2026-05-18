#!/usr/bin/env bash
# Build & deploy gastrobrain to Cloud Run (asia-northeast1).
# Run AFTER deploy/secrets.sh.

set -euo pipefail

PROJECT="${GCP_PROJECT:-gastrobrain-production}"
REGION="${GCP_REGION:-asia-northeast1}"
SERVICE="${GCP_SERVICE:-gastrobrain}"

gcloud config set project "$PROJECT" >/dev/null
gcloud config set run/region "$REGION" >/dev/null

echo "Project: $PROJECT  Region: $REGION  Service: $SERVICE"
echo ""

OPTIONAL_SECRETS=()
for s in LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
  if gcloud secrets describe "$s" >/dev/null 2>&1; then
    OPTIONAL_SECRETS+=("$s=$s:latest")
  fi
done

SECRET_ARGS=(
  "DATABASE_URL=DATABASE_URL:latest"
  "CLAUDE_API_KEY=CLAUDE_API_KEY:latest"
  "COHERE_API=COHERE_API:latest"
  "SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest"
  "SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest"
  "SUPABASE_JWT_SECRET=SUPABASE_JWT_SECRET:latest"
  "SUPABASE_PROJECT_URL=SUPABASE_PROJECT_URL:latest"
  "${OPTIONAL_SECRETS[@]}"
)

# Comma-join secret args
SECRETS_CSV=$(IFS=,; echo "${SECRET_ARGS[*]}")

echo "Deploying (this triggers Cloud Build — first run takes ~3 min)..."

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --no-cpu-throttling \
  --cpu-boost \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 3 \
  --timeout 60 \
  --concurrency 8 \
  --set-env-vars "ENV=prod,LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com,ANTHROPIC_MODEL=claude-sonnet-4-6,EMBEDDING_MODEL=embed-multilingual-v3.0,RERANK_MODEL=rerank-multilingual-v3.0" \
  --set-secrets "$SECRETS_CSV"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo ""
echo "==============================================="
echo "Service URL: $URL"
echo ""
echo "Update Slack app at https://api.slack.com/apps with these Request URLs:"
echo "  Slash command (/gastrobrain) → $URL/slack/commands"
echo "  Event Subscriptions          → $URL/slack/events"
echo "  Interactivity & Shortcuts    → $URL/slack/interactive"
echo ""
echo "Then reinstall the app to the workspace."
echo "==============================================="
