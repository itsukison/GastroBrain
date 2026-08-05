#!/usr/bin/env bash
# Deploy the nightly Slack sync as a Cloud Run Job + Cloud Scheduler trigger.
# Idempotent — safe to re-run.
#
# Scope: ALL channels the bot can read — every public channel (self-joined) plus
# every private channel it has been invited to. gb-slack-ingest is idempotent per
# day-doc (content_hash), so re-runs only embed days that gained messages. After
# ingest, gb-slack-acl-sync mirrors channel membership so retrieval gates each
# user to the channels their Slack account can actually access (public channels
# are visible to everyone; private channels only to their members). The ACL sync
# runs AFTER ingest so it sees any newly-created channel rows.
#
# Requires: DATABASE_URL / COHERE_API / SLACK_BOT_TOKEN secrets (deploy/secrets.sh).
# The Slack app needs the users:read.email scope so the ACL sync can link
# members.slack_user_id by email (without it, private-channel gating falls back
# to public-only for web/MCP callers).

set -euo pipefail

PROJECT="${GCP_PROJECT:-gastrobrain-production}"
REGION="${GCP_REGION:-asia-northeast1}"
JOB="${GCP_JOB:-gastrobrain-slack-sync}"
SCHEDULE="${SYNC_SCHEDULE:-0 3 * * *}"   # 03:00 JST nightly
SA="${SCHEDULER_SA:-gastrobrain-deploy-986@gastrobrain-production.iam.gserviceaccount.com}"

gcloud config set project "$PROJECT" >/dev/null
echo "Project: $PROJECT  Region: $REGION  Job: $JOB  (all channels)"

echo "Deploying Cloud Run job (triggers Cloud Build)..."
gcloud run jobs deploy "$JOB" \
  --source . \
  --region "$REGION" \
  --command /bin/sh \
  --args="-c,gb-slack-ingest --all-channels && gb-slack-acl-sync" \
  --memory 1Gi \
  --cpu 1 \
  --task-timeout 3600 \
  --max-retries 1 \
  --set-env-vars "ENV=prod,EMBEDDING_MODEL=embed-multilingual-v3.0" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,COHERE_API=COHERE_API:latest,SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest"

echo "Creating/updating Cloud Scheduler trigger ($SCHEDULE JST)..."
SCHEDULER_ARGS=(
  --location "$REGION"
  --schedule "$SCHEDULE"
  --time-zone "Asia/Tokyo"
  --uri "https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$JOB:run"
  --http-method POST
  --oauth-service-account-email "$SA"
)
if gcloud scheduler jobs describe "$JOB-nightly" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$JOB-nightly" "${SCHEDULER_ARGS[@]}"
else
  gcloud scheduler jobs create http "$JOB-nightly" "${SCHEDULER_ARGS[@]}"
fi

echo ""
echo "Done. Manual trigger: gcloud run jobs execute $JOB --region $REGION"
