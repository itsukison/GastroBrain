# Gastrobrain — Slack Deploy Checklist

**Time:** ~30–45 min, mostly waiting for first Cloud Build.
**Prereqs:** GCP project `gastrobrain-prod` exists, billing linked, APIs enabled (done in SETUP.md §2).

---

## §1. Install gcloud CLI (~5 min, one-time)

Install gcloud **outside** the project directory (the SDK is ~440MB and should not live in the repo):

```bash
# Option A — Homebrew (recommended)
brew install --cask google-cloud-sdk

# Option B — manual install into ~/
cd ~
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-arm.tar.gz
tar -xf google-cloud-cli-darwin-arm.tar.gz
./google-cloud-sdk/install.sh
rm google-cloud-cli-darwin-arm.tar.gz
```

Restart your terminal, then verify: `gcloud --version`.

---

## §2. Authenticate (~2 min)

```bash
gcloud auth login                             # opens browser; sign in with your gastroduce-japan account
gcloud config set project gastrobrain-prod
gcloud auth application-default login         # for local development access (optional but recommended)
```

---

## §3. Push secrets to Secret Manager (~5 min)

This runs interactively and prompts you for each secret. Values are pasted directly into Secret Manager — they don't appear in your shell history.

```bash
cd /Users/gastroduce/Desktop/gastro
./deploy/secrets.sh
```

You'll be asked for, in order:
1. `DATABASE_URL` — same Postgres URI from your `.env` (copy verbatim).
2. `CLAUDE_API_KEY` — same as your `.env`.
3. `COHERE_API` — same as your `.env`.
4. `SLACK_BOT_TOKEN` — the **rotated** `xoxb-...` token from 1Password.
5. `SLACK_SIGNING_SECRET` — just the **hex value**, no `signing secret:` prefix. (If your `.env` still has the prefix, paste only the hex part starting at `8e73dd...`.)
6. `LANGFUSE_PUBLIC_KEY` — optional, hit Enter to skip.
7. `LANGFUSE_SECRET_KEY` — optional, hit Enter to skip.

The script also grants the Cloud Run runtime service account permission to read the secrets.

---

## §4. Deploy (~3 min first time, ~1 min thereafter)

```bash
./deploy/run.sh
```

This builds the container via Cloud Build and deploys to Cloud Run in Tokyo. When it finishes, it prints:

```
Service URL: https://gastrobrain-XXXXXX-an.a.run.app

Update Slack app at https://api.slack.com/apps with these Request URLs:
  Slash command (/gastrobrain) → <URL>/slack/commands
  Event Subscriptions          → <URL>/slack/events
  Interactivity & Shortcuts    → <URL>/slack/interactive
```

**Copy the URL — you'll paste it into Slack next.**

Quick sanity check:
```bash
curl https://gastrobrain-XXXXXX-an.a.run.app/healthz
# → {"status":"ok"}
```

---

## §5. Update the Slack app (~5 min)

Open https://api.slack.com/apps → **Gastrobrain**.

### 5.1 Slash Commands
- Click `/gastrobrain`
- Replace **Request URL** with `<service-url>/slack/commands`
- Save

### 5.2 Event Subscriptions
- Toggle "Enable Events" if it's off
- Replace **Request URL** with `<service-url>/slack/events`
- Slack will hit the URL; should show **"Verified ✓"** within a few seconds (this is the URL verification handshake — already wired)
- Subscribe to bot events (if not already):
  - `app_mention`
  - `message.im`
- Save

### 5.3 Interactivity & Shortcuts (this is **new** — wasn't in the original setup)
- Toggle **"Interactivity"** ON
- **Request URL:** `<service-url>/slack/interactive`
- Save Changes

### 5.4 Reinstall to workspace
After changing scopes/events/interactivity, Slack requires reinstall:
- **OAuth & Permissions** → "Reinstall to Workspace" → Allow
- The bot token does **not** change on reinstall (your stored secret stays valid).

---

## §6. Test in Slack (~5 min)

In any channel or DM, try all three surfaces:

| Surface | Try |
|---|---|
| Slash command | `/gastrobrain TTSのデイリーチェックリストには何が含まれていますか？` |
| @mention | Add the bot to a channel, then `@Gastrobrain TikTok LIVEの禁止行為は？` |
| DM | Open a DM with Gastrobrain, type the question directly |

Each response should:
1. First show "🤔 考え中..."
2. Update with the answer + 出典 list + **👍 役立った / 👎 改善が必要** buttons
3. Clicking a button updates the message to show "_<user> が 👍 を記録しました_" — that's the feedback persisted to `queries.feedback` in Supabase.

---

## §7. Roll out to managers

Once it works for you:
- Send the bot user ID to managers (or @-mention them in a channel introducing it).
- Tell them the three ways to use it (slash, @mention, DM).
- Ask them to use the buttons — that's how we'll tell what's working.

You can pull a feedback summary from Supabase any time:

```sql
SELECT
  feedback,
  count(*) AS n,
  round(avg(latency_ms))::int AS avg_ms
FROM queries
WHERE asked_at > now() - interval '7 days'
GROUP BY feedback
ORDER BY feedback;
```

Or list the 👎-rated questions to triage:

```sql
SELECT asked_at, user_id, question, answer
FROM queries
WHERE feedback = -1
ORDER BY asked_at DESC
LIMIT 50;
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Slack shows "Request URL did not respond in time" on Verify | Cold start — Cloud Run was scaled to zero | Hit `<url>/healthz` once to warm it, then click Verify again |
| `/gastrobrain` shows "🤔 考え中..." but never updates | Cloud Run is throttling background tasks | Confirm `--no-cpu-throttling` is set (it is in `run.sh`); check Cloud Run logs |
| Buttons don't work | Interactivity URL not configured | §5.3 above |
| Bot doesn't reply to @mention | Not invited to channel, OR `app_mention` not subscribed | Add bot to channel; check Event Subscriptions |
| 403 in Cloud Run logs | Slack signing secret mismatch | Re-run `./deploy/secrets.sh`, paste the correct secret, then `./deploy/run.sh` |
| "missing required env vars" in logs | A secret wasn't created or IAM binding missing | Check `gcloud secrets list`, re-run `secrets.sh` |

Logs:
```bash
gcloud run services logs read gastrobrain --region asia-northeast1 --limit 100
```

---

## What you need to send me when done

Just one thing:
1. ✅ Bot is responding in Slack (or what's broken)

If it works, ping me with the Cloud Run URL and we'll set up monitoring + cost alerting next.
