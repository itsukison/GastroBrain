#!/usr/bin/env bash
# One-time: create Secret Manager entries that Cloud Run will mount.
# Run this AFTER gcloud auth login + gcloud config set project gastrobrain-production.
#
# This script is interactive — it'll prompt you for each secret's value so
# nothing ends up in shell history. Pasted values are sent directly to
# Secret Manager and never echoed.

set -euo pipefail

PROJECT="${GCP_PROJECT:-gastrobrain-production}"
gcloud config set project "$PROJECT" >/dev/null

echo "Project: $PROJECT"
echo "This will create / update the following secrets in Secret Manager:"
echo "  - DATABASE_URL"
echo "  - CLAUDE_API_KEY"
echo "  - COHERE_API"
echo "  - SLACK_BOT_TOKEN"
echo "  - SLACK_SIGNING_SECRET"
echo "  - LANGFUSE_PUBLIC_KEY  (optional, leave blank to skip)"
echo "  - LANGFUSE_SECRET_KEY  (optional, leave blank to skip)"
echo "  - GASTROBRAIN_MCP_TOKENS  (optional, leave blank to disable /mcp)"
echo "  - GOOGLE_OAUTH_CLIENT_ID  (optional, for /mcp/ OAuth)"
echo "  - GOOGLE_OAUTH_CLIENT_SECRET  (optional, for /mcp/ OAuth)"
echo "  - GASTROBRAIN_OAUTH_JWT_KEY  (optional, auto-mint if blank)"
echo "  - GASTROBRAIN_OAUTH_STATE_KEY  (optional, auto-mint if blank)"
echo ""

create_or_update() {
  local name="$1"
  local prompt="$2"
  echo ""
  read -rsp "$prompt: " value
  echo ""
  if [[ -z "$value" ]]; then
    echo "  (skipped $name — no value entered)"
    return
  fi
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
    echo "  ✓ updated $name (new version)"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
    echo "  ✓ created $name"
  fi
}

create_or_update DATABASE_URL          "Postgres URI (postgresql://postgres.<ref>:<password>@aws-1-...pooler.supabase.com:5432/postgres)"
create_or_update CLAUDE_API_KEY        "Anthropic API key (sk-ant-...)"
create_or_update COHERE_API            "Cohere API key"
create_or_update SLACK_BOT_TOKEN       "Slack bot token (xoxb-... — use the ROTATED one)"
create_or_update SLACK_SIGNING_SECRET  "Slack signing secret (just the hex, no 'signing secret:' prefix)"
create_or_update LANGFUSE_PUBLIC_KEY   "Langfuse public key (optional, blank to skip)"
create_or_update LANGFUSE_SECRET_KEY   "Langfuse secret key (optional, blank to skip)"
create_or_update GASTROBRAIN_MCP_TOKENS "MCP bearer tokens, 'label:tok_xxx,label2:tok_yyy' (blank to disable /mcp)"
create_or_update GOOGLE_OAUTH_CLIENT_ID     "Google OAuth client ID (xxxxxx.apps.googleusercontent.com)"
create_or_update GOOGLE_OAUTH_CLIENT_SECRET "Google OAuth client secret"

mint_if_absent() {
  local name="$1"
  local prompt="$2"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    echo "  (already exists $name — leaving as is)"
    return
  fi
  echo ""
  read -p "$prompt [auto-mint a fresh 64-byte key]? [y/N] " yn
  if [[ "$yn" =~ ^[Yy]$ ]]; then
    local val
    val="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
    printf '%s' "$val" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
    echo "  ✓ created $name (auto-minted)"
  else
    echo "  (skipped $name)"
  fi
}

mint_if_absent GASTROBRAIN_OAUTH_JWT_KEY    "Auto-mint OAuth JWT signing key"
mint_if_absent GASTROBRAIN_OAUTH_STATE_KEY  "Auto-mint OAuth state-token signing key"

echo ""
echo "Granting Cloud Run runtime SA access to read these secrets..."
SA="$(gcloud iam service-accounts list --filter='displayName:Default compute service account' --format='value(email)' | head -1)"
if [[ -z "$SA" ]]; then
  SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
fi
echo "  Cloud Run SA: $SA"

for s in DATABASE_URL CLAUDE_API_KEY COHERE_API SLACK_BOT_TOKEN SLACK_SIGNING_SECRET LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY GASTROBRAIN_MCP_TOKENS GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET GASTROBRAIN_OAUTH_JWT_KEY GASTROBRAIN_OAUTH_STATE_KEY; do
  if gcloud secrets describe "$s" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "$s" \
      --member="serviceAccount:$SA" \
      --role="roles/secretmanager.secretAccessor" >/dev/null 2>&1 || true
  fi
done
echo "  ✓ IAM bindings applied"
echo ""
echo "Done. Now run: deploy/run.sh"
