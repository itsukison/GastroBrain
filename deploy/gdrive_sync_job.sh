#!/usr/bin/env bash
# Deploy the nightly Google Drive transcript sync (add/update/delete) as a
# Cloud Run Job + Cloud Scheduler trigger. Idempotent — safe to re-run.
#
# Requires: DATABASE_URL / COHERE_API secrets (created by deploy/secrets.sh).
#
# Drive auth: the JOB runs *as* $SA (--service-account), and gdrive.py reads
# the folder with that SA's own credentials (drive.readonly scope). PREREQUISITE:
# the Shared Drive holding 会議録 must be shared (Viewer) with $SA's email, and
# the Drive API enabled on the project. If your Workspace blocks that, set
# GDRIVE_IMPERSONATE_SA instead (see gdrive.py header).

set -euo pipefail

PROJECT="${GCP_PROJECT:-gastrobrain-production}"
REGION="${GCP_REGION:-asia-northeast1}"
JOB="${GCP_JOB:-gastrobrain-gdrive-sync}"
SCHEDULE="${SYNC_SCHEDULE:-0 3 * * *}"   # 03:00 JST nightly
SA="${SCHEDULER_SA:-gastrobrain-deploy-986@gastrobrain-production.iam.gserviceaccount.com}"

gcloud config set project "$PROJECT" >/dev/null
echo "Project: $PROJECT  Region: $REGION  Job: $JOB  Runtime SA: $SA"

echo "Deploying Cloud Run job (triggers Cloud Build)..."
gcloud run jobs deploy "$JOB" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --command gb-drive-ingest \
  --memory 1Gi \
  --cpu 1 \
  --task-timeout 3600 \
  --max-retries 1 \
  --set-env-vars "ENV=prod,EMBEDDING_MODEL=embed-multilingual-v3.0" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,COHERE_API=COHERE_API:latest"

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
