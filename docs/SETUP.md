# Gastrobrain — Manual Setup Runbook

**Audience:** Itsuki Son (project owner)
**Purpose:** the work *you* need to do by hand before/while engineering builds Gastrobrain. Each section is a checklist. Don't skip steps.

---

## §1. Slack workspace + app setup (OQ-3)

This unblocks Phase 0. Total time: ~30 minutes once you have admin access.

### 1.1 Confirm you have workspace admin

- [ ] Open Slack, click the workspace name top-left → "Settings & administration" → "Manage members". If you can see members and roles, you're an Admin or Owner. If not: ask whoever is (likely the CEO) to either grant you Admin, or to perform §1.2–1.4 with you on a screen-share.
- [ ] Note the workspace name and URL (e.g., `gastroduce.slack.com`). Send it to me when done.

### 1.2 Create the Slack app

- [ ] Go to https://api.slack.com/apps and click **"Create New App"**.
- [ ] Choose **"From scratch"**.
- [ ] App name: `Gastrobrain` (or `ガストブレイン` if you prefer JP).
- [ ] Pick the Gastroduce workspace.
- [ ] Click Create.

### 1.3 Configure OAuth & permissions

In the app settings sidebar, go to **"OAuth & Permissions"** and add the following Bot Token Scopes (do **not** add User Token Scopes):

- [ ] `app_mentions:read` — receive @-mentions
- [ ] `chat:write` — post messages
- [ ] `chat:write.public` — post in channels the bot isn't a member of
- [ ] `commands` — register slash commands
- [ ] `im:history` — read DMs sent to the bot
- [ ] `im:read` — see DM channel info
- [ ] `im:write` — open DMs
- [ ] `users:read` — resolve Slack user IDs to names (for query logging)

### 1.4 Enable features

- [ ] **Slash Commands** → Create New Command:
  - Command: `/gastrobrain` (or `/brain`)
  - Request URL: leave as `https://example.com/slack/commands` for now — engineering will replace this with the real Cloud Run URL later.
  - Short description: `社内ナレッジに質問する`
  - Usage hint: `[質問を入力]`
- [ ] **Event Subscriptions** → Enable Events:
  - Request URL: leave placeholder for now.
  - Subscribe to bot events: `app_mention`, `message.im`.
- [ ] **App Home** → enable Messages tab, allow users to send messages from the messages tab.

### 1.5 Install to workspace (do NOT distribute)

- [ ] OAuth & Permissions → "Install to Workspace" → review scopes → Allow.
- [ ] After install, copy these two secrets and **paste them into 1Password / your password manager — do not email or Slack them to me**:
  - **Bot User OAuth Token** (starts with `xoxb-`)
  - **Signing Secret** (Basic Information → App Credentials)
- [ ] Send me the **App ID** and **Workspace ID** (in any channel — these are not secrets). Workspace ID can be retrieved at: workspace name → About this workspace.

### 1.6 What I need from you to unblock Phase 0

A short message containing:
1. ✅ Slack admin confirmed
2. App ID: `A0XXXXXXXX`
3. Workspace ID: `T0XXXXXXXX`
4. Bot token + Signing secret saved in 1Password (just confirm yes/no — do not paste)

---

## §2. GCP project setup (OQ-6)

Total time: ~20 minutes if your company has an existing Google Workspace + billing account, ~1 hour if starting from scratch.

### 2.1 Decide who owns the GCP organization

- [ ] Does Gastroduce already have a GCP organization linked to its Google Workspace domain (`gastroduce-japan.co.jp`)? Check at https://console.cloud.google.com/cloud-resource-manager — if you see an org node with the company name, yes.
- [ ] If **no organization exists**: ask the IT/finance owner to create one (this is a one-time setup that affects all future GCP usage). It requires Google Workspace super-admin to confirm domain ownership. Don't skip this — projects under an org are easier to govern than personal projects.
- [ ] If **an organization exists**: ask whoever owns it (likely IT or CTO) to grant you `roles/resourcemanager.projectCreator` on the org. Without this, you can't make projects.

### 2.2 Create the project

- [ ] Open https://console.cloud.google.com/projectcreate
- [ ] Project name: `Gastrobrain Production` (display name)
- [ ] Project ID: `gastrobrain-prod` (must be globally unique — if taken, use `gastrobrain-gastroduce-prod`)
- [ ] Organization: select Gastroduce
- [ ] Location: the org root (or whichever folder IT designates)
- [ ] Click Create.

### 2.3 Set up billing

- [ ] Get a billing account ID from finance. If Gastroduce has an existing GCP billing account (commonly named after the company), get its ID. If not, finance needs to create one with a corporate credit card at https://console.cloud.google.com/billing.
- [ ] Link the billing account: Project → Billing → Link a billing account.
- [ ] **Set a budget alert** (this is the line of defense against runaway costs):
  - Budget name: `Gastrobrain Monthly`
  - Amount: ¥300,000/month (50% buffer over our ¥230k projection)
  - Alerts at 50%, 90%, 100% — sent to your email.

### 2.4 Enable required APIs

In the project, run these (or click through the console — **Library** → search → enable):

- [ ] Cloud Run API (`run.googleapis.com`)
- [ ] Cloud Tasks API (`cloudtasks.googleapis.com`)
- [ ] Cloud Build API (`cloudbuild.googleapis.com`)
- [ ] Secret Manager API (`secretmanager.googleapis.com`)
- [ ] Cloud Logging API (`logging.googleapis.com`)
- [ ] Cloud Monitoring API (`monitoring.googleapis.com`)
- [ ] Artifact Registry API (`artifactregistry.googleapis.com`)
- [ ] Cloud Scheduler API (`cloudscheduler.googleapis.com`) — for the drift reconciliation cron

If you have `gcloud` installed locally, this one command does all of them:
```
gcloud services enable run.googleapis.com cloudtasks.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com logging.googleapis.com monitoring.googleapis.com artifactregistry.googleapis.com cloudscheduler.googleapis.com --project=gastrobrain-prod
```

### 2.5 Create the deploy service account

- [ ] IAM → Service Accounts → Create Service Account
  - Name: `gastrobrain-deploy`
  - Roles: `Cloud Run Admin`, `Service Account User`, `Artifact Registry Writer`, `Secret Manager Secret Accessor`
- [ ] Service Accounts → `gastrobrain-deploy` → Keys → Add Key → JSON. **Save the JSON to 1Password** — this is what engineering will use for CI/CD. Do not commit it anywhere.

### 2.6 Set the region

Default region for everything Gastrobrain creates: **`asia-northeast1` (Tokyo)**. This is mostly handled in code/config, but worth confirming the project has no override.

### 2.7 What I need from you to unblock Phase 0

A short message containing:
1. ✅ Project created. Project ID: `gastrobrain-prod` (or whatever you used)
2. ✅ Billing linked. Budget alert at ¥300k/month set.
3. ✅ APIs enabled.
4. ✅ Deploy service account JSON saved in 1Password.

---

## §3. Golden-set interviews — CEO + COO (OQ-4)

This work happens during Phase 3 (weeks 4–5), not Phase 0. But you can schedule the meetings now since calendars fill up.

### 3.1 Schedule

- [ ] Book **45 minutes each** with CEO and COO. Separate sessions, not joint — you want their unfiltered views, not consensus.
- [ ] Send the briefing email below ahead of time so they show up prepared. Without prep they'll give you 5 generic questions; with prep they'll give you 20 sharp ones.

### 3.2 Briefing email (copy-paste, edit names)

> Subject: Gastrobrain (社内AIアシスタント) ヒアリングのお願い — 45分
>
> [CEO/COO] さん、
>
> NotePM上の社内ナレッジに自然言語で質問できるAI（Gastrobrain）の構築を進めています。品質を測るための「正解集 (golden set)」を作るにあたり、45分ほどお時間をいただきたいです。
>
> **当日までに考えておいていただきたいこと（事前準備5分）:**
> 直近3ヶ月で、社員から繰り返し聞かれた質問、または「これNotePM見ればわかるのに」と思った質問を、思い出せる範囲で書き出してください。完璧でなくて構いません。10〜20個あれば十分です。
>
> 例:
> - 「EC在庫の最低発注量はどう決めている？」
> - 「新規取引先の与信審査フローは？」
> - 「○○商品の昨年の売上推移は？」
>
> 当日は内容を一緒に整理し、AIが正しく答えられるかの基準にします。
>
> よろしくお願いします。

### 3.3 Interview structure (45 min)

| Block | Time | Goal |
|---|---|---|
| Warmup | 5 min | Explain how Gastrobrain will work in 2 sentences. Reassure: this is for *measuring quality*, their answers won't be exposed to anyone. |
| Question dump | 20 min | They read out their list. You transcribe verbatim. **Do not edit or "improve" the questions** — bad/vague phrasing is realistic data. |
| Probe for context | 10 min | For each question, ask: "what's the ideal answer?" and "where in NotePM should it be?" If they say "it's not written down anywhere" → flag as `corpus-gap` and skip. |
| Adversarial pass | 10 min | Ask: "what kind of question would you *not* trust an AI to answer?" "What would be embarrassing if it got wrong?" These become the adversarial test cases. |

### 3.4 Output format

After both interviews, produce a CSV with columns:

| question | expected_answer | expected_source_url | difficulty | category | source |
|---|---|---|---|---|---|
| EC在庫の最低発注量はどう決めている？ | … | https://…/notepm/page/123 | medium | EC運用 | CEO |

Aim for 40 entries total (~20 from each). Engineering will combine these with the 100 Slack-sourced + 60 adversarial questions to make the 200-question set.

### 3.5 What I need from you

- [ ] Both interviews scheduled (you can do them anytime in weeks 1–4; doesn't block Phase 0).
- [ ] Once done, the CSV. Send it as an attachment, not pasted into chat.

---

## §4. Summary — what's blocking Phase 0

To start engineering work, I need from you (in order of urgency):

1. **GCP project ID + confirmation that the deploy service account key is in 1Password** (§2.7) — blocks all infra work.
2. **Slack App ID + Workspace ID + confirmation that bot token + signing secret are in 1Password** (§1.6) — blocks the Slack surface but not the ingestion pipeline.
3. **CEO/COO interviews scheduled** (§3) — does not block Phase 0; just needs to land before week 5.

Other items (Cohere API key, Anthropic API key, Supabase project, NotePM API token, Langfuse account) — I'll generate a second checklist for those once Phase 0 starts. They're cheap and quick once the GCP foundation is in place.
