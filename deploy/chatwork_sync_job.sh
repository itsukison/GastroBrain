#!/usr/bin/env bash
# Deploy the nightly Chatwork sync as a Cloud Run Job + Cloud Scheduler trigger.
# Idempotent — safe to re-run.
#
# Scope: the operator-curated room allowlist (CHATWORK_ROOM_IDS). The Chatwork
# API is forward-only — each run can only see the latest ~100 messages per room
# (no history pagination) — so the nightly cadence is what accumulates history
# over time. gb-chatwork-ingest is idempotent per day-doc (content_hash), so
# re-runs only embed days that gained messages.
#
# No ACL sync: Chatwork exposes no member emails and has no public-room concept,
# so docs can't be gated per user. Only allowlisted rooms are ingested and they
# are visible to every signed-in user (unrestricted, like gdrive). Curate
# CHATWORK_ROOM_IDS accordingly — do not add private/sensitive rooms.
#
# Requires: DATABASE_URL / COHERE_API / CHATWORK_API secrets (deploy/secrets.sh)
# and the CHATWORK_ROOM_IDS env var (comma-separated room_ids).

set -euo pipefail

PROJECT="${GCP_PROJECT:-gastrobrain-production}"
REGION="${GCP_REGION:-asia-northeast1}"
JOB="${GCP_JOB:-gastrobrain-chatwork-sync}"
SCHEDULE="${SYNC_SCHEDULE:-0 3 * * *}"   # 03:00 JST nightly
SA="${SCHEDULER_SA:-gastrobrain-deploy-986@gastrobrain-production.iam.gserviceaccount.com}"
ROOM_IDS="${CHATWORK_ROOM_IDS:?Set CHATWORK_ROOM_IDS (comma-separated room_ids) before deploying}"

gcloud config set project "$PROJECT" >/dev/null
echo "Project: $PROJECT  Region: $REGION  Job: $JOB  Rooms: $ROOM_IDS"

echo "Deploying Cloud Run job (triggers Cloud Build)..."
gcloud run jobs deploy "$JOB" \
  --source . \
  --region "$REGION" \
  --command /bin/sh \
  --args="-c,gb-chatwork-ingest" \
  --memory 1Gi \
  --cpu 1 \
  --task-timeout 3600 \
  --max-retries 1 \
  --set-env-vars "ENV=prod,EMBEDDING_MODEL=embed-multilingual-v3.0,CHATWORK_ROOM_IDS=$ROOM_IDS" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,COHERE_API=COHERE_API:latest,CHATWORK_API=CHATWORK_API:latest"

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
