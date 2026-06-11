#!/usr/bin/env bash
# Deploy the nightly NotePM sync (add/update/delete) as a Cloud Run Job
# + Cloud Scheduler trigger. Idempotent — safe to re-run on changes.
#
# Requires: notepm-api-token secret (already used by the MCP server),
# plus DATABASE_URL / COHERE_API (created by deploy/secrets.sh).

set -euo pipefail

PROJECT="${GCP_PROJECT:-gastrobrain-production}"
REGION="${GCP_REGION:-asia-northeast1}"
JOB="${GCP_JOB:-gastrobrain-notepm-sync}"
SCHEDULE="${SYNC_SCHEDULE:-0 3 * * *}"   # 03:00 JST nightly
SA="${SCHEDULER_SA:-gastrobrain-deploy-986@gastrobrain-production.iam.gserviceaccount.com}"

gcloud config set project "$PROJECT" >/dev/null
echo "Project: $PROJECT  Region: $REGION  Job: $JOB"

echo "Deploying Cloud Run job (triggers Cloud Build)..."
# Runs the content sync, then the access (ACL) sync — in that order so the ACL
# sync sees freshly-ingested note_code values and the latest NotePM notes/groups.
gcloud run jobs deploy "$JOB" \
  --source . \
  --region "$REGION" \
  --command /bin/sh \
  --args "-c,gb-notepm-ingest && gb-notepm-acl-sync" \
  --memory 1Gi \
  --cpu 1 \
  --task-timeout 3600 \
  --max-retries 1 \
  --set-env-vars "ENV=prod,EMBEDDING_MODEL=embed-multilingual-v3.0,NOTEPM_MANAGERS_FILE=/app/config/notepm_managers.yaml,NOTEPM_EXCLUDED_NOTES_FILE=/app/config/notepm_excluded_notes.yaml" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,COHERE_API=COHERE_API:latest,NOTEPM_API_TOKEN=notepm-api-token:latest"

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
