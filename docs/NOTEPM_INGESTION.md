# NotePM ingestion — status & runbook

**Last updated:** 2026-06-01

This doc covers the NotePM-source half of the ingestion pipeline (PRD §4).
The directory-scan path (`gb-ingest corpus/`) is documented separately in
`corpus/README.md` and is unchanged by this work.

## 1. Status

**Built and shipped:**
- NotePM API client — `src/gastrobrain/notepm.py` (incl. `list_notes()` for folder_path).
- Manager roster + cutoff + excluded-notes config — `config/notepm_managers.yaml`,
  `config/notepm_excluded_notes.yaml`, plus five fields in `src/gastrobrain/config.py`
  (`notepm_api_token`, `notepm_team_subdomain`, `notepm_cutoff_date`,
  `notepm_managers_file`, `notepm_excluded_notes_file`).
- Dry-run CLI — `gb-notepm-dryrun` (`gastrobrain.notepm_cli:cli`).
- Ingestion CLI — `gb-notepm-ingest` (`gastrobrain.notepm_cli:ingest_cli`).
  Reuses `chunker.chunk_markdown` and `embed.embed_texts` from the manual-ingest
  path. Writes to `documents`/`chunks` with `source='notepm'`. Idempotent via
  `content_hash`; safe to re-run.
- Hardening done during backfill: 60s httpx timeout on Cohere, `TransportError`
  added to retry whitelist on both `notepm.py` and `embed.py`, `psycopg`
  connection-pool health check.

**Backfill progress (live numbers, queried against Supabase 2026-06-01):**
- **5,608 NotePM documents** ingested; **75,084 chunks** (all embedded).
- Date range covered: `2025-06-02` … `2026-05-29` (≈12 months).
- 46 distinct notebooks represented (post-exclusion).
- 0 soft-deleted rows.

**Backfill remaining:**
- `notepm_cutoff_date` in config is **`2024-05-26`** (2-year window).
- Oldest ingested page is `2025-06-02`, so **~12 months / ~5–6k pages** older
  than that are still un-ingested. See §8 for the run command.

**Cohere key state (see also memory `feedback_cohere_billing.md`):**
- Two prior keys turned out to be trial-tier (1,000 calls/month cap) and
  blocked previous attempts.
- A real Production-tier key is now in `gastrobrain-production` Secret Manager
  `COHERE_API`. Bulk ingestion is no longer gated on Itsuki's personal card.

**Not yet built:**
- Webhook handler for incremental sync (PRD §4.2).
- Drift reconciliation cron (PRD §4.2.1).
- Google Drive transcripts (PRD §10.1).

## 2. Design decisions taken 2026-05-25

| Decision | Value | Rationale |
|---|---|---|
| Time window | **Fixed cutoff** at `2024-05-26` (2-year window from 2026-05-26) | Started at 2025-11-25 (6 months); widened to 2 years on 2026-05-26 after the real Production-tier key landed. Switching to rolling later is ~20 lines and one cron — see §6. |
| Author filter | Page-level `created_by` OR `updated_by` user_code is in the manager list | A manager editing a junior's page is a strong endorsement signal. A junior fixing a typo on a manager's page doesn't disqualify the page (still passes via `created_by`). |
| Manager roster | Hand-curated YAML in repo (`config/notepm_managers.yaml`) | Itsuki provided a one-time list. PR-reviewed changes give an audit trail. HR-self-service can be added later when scale demands it. |
| Match precedence | `name_ja` → `name_en` → `email` (NFKC + whitespace-stripped + lowercased) | Tolerates full-width vs half-width spaces (`草壁　匠` ↔ `草壁 匠`), trailing-spaces, kanji variants. Email is the hardest identifier and acts as the fallback. |
| Rate limit | Token bucket at **50 req/min** (NotePM caps at 60/min — PRD §4.1) | Leaves 10 req/min headroom for concurrent webhook traffic once it ships. |
| HTTP timeout | 90s read, 10s connect, tenacity-retried on `429`, `5xx`, `TimeoutException`, `NetworkError` | First live scan hit a `ReadTimeout` on a 100-page response. NotePM occasionally takes >30s to serve full-body batches. |

## 3. Live numbers

**Ingested state (Supabase, 2026-06-01):**

| Metric | Value |
|---|---|
| NotePM documents (active, `source='notepm'`) | **5,608** |
| Chunks (all embedded) | **75,084** |
| Date range covered (`updated_at`) | `2025-06-02` … `2026-05-29` |
| Distinct notebooks represented | 46 |
| Soft-deleted | 0 |

**Filter parameters in effect:**
- Cutoff: `2024-05-26` (configured) — still ~12 months of older pages to fill.
- Managers: 16 resolved (see §5).
- Excluded notebooks: 8 (HR, accounting, legal — see `config/notepm_excluded_notes.yaml`).

Re-run `gb-notepm-dryrun` before the next phase of the backfill to get a fresh
count of remaining matching pages — the corpus grows daily and the older
range hasn't been scanned for matches yet.

## 4. NotePM API reference (verified empirically)

Base URL: `https://gastroduce-jp.notepm.jp/api/v1`
Auth: `Authorization: Bearer <token>` (token from Secret Manager — see §7).
Pagination meta: `{ "previous_page": null|url, "next_page": null|url, "page": N, "per_page": N, "total": N }`.

### `GET /users?page=N&per_page=100`

Response shape (relevant fields only):
```json
{
  "users": [
    {
      "user_code": "1259561608",
      "name": "homu",
      "auth_class": "user",
      "email": "risa.katsube@gastroduce-japan.co.jp",
      "status": "normal",
      "last_login_date": "2026-05-25T12:14:51+09:00",
      "created_at": "2023-03-17T14:15:37+09:00"
    }
  ],
  "meta": { ... }
}
```

### `GET /pages?page=N&per_page=100`

Returns full bodies inline. Default order is **newest-first by `updated_at`**.
No `updated_since`-style filter param visible — we filter client-side and
break out of pagination when `updated_at < cutoff`.

```json
{
  "pages": [
    {
      "page_code": "70493384a3",
      "note_code": "b16296da38",
      "folder_id": null,
      "title": "...",
      "body": "markdown with \\r\\n line endings and embedded <img> tags",
      "created_at": "2026-05-25T09:57:11+09:00",
      "updated_at": "2026-05-25T12:30:31+09:00",
      "created_by": { "user_code": "5035854826", "name": "渡邉大輔" },
      "updated_by": { "user_code": "0587048116", "name": "草壁 匠" },
      "tags": []
    }
  ],
  "meta": { ... }
}
```

Permalink to a page is `https://gastroduce-jp.notepm.jp/page/<page_code>`
(constructed; the API does not return a URL field).

### NotePM terminology

| API word | Meaning |
|---|---|
| `note` | Top-level notebook / workspace. `note_code` identifies it. Has `groups` (role-based ACLs like 部長以上) and explicit `users`. |
| `folder` | Folder within a note. `folder_id` is numeric, may be null for root-level pages. |
| `page` | The actual content document — what we ingest as a `document` in our schema. |

## 5. Manager → NotePM user_code mapping

Verified 2026-05-25 by the dry-run. If a NotePM display name changes,
update `config/notepm_managers.yaml` and re-run `gb-notepm-dryrun`.

| YAML `name_ja` | NotePM `name` | `user_code` | Email |
|---|---|---|---|
| (none, matched by email) | 孫逸歓 | `2997342857` | itsuki.son@gastroduce-japan.co.jp |
| 江頭 舞 | 江頭　舞 | `5177857410` | mai.egashira@gastroduce-japan.com |
| 高山恵理 | 高山恵理 | `0105546876` | eri.takayama@gastroduce-japan.co.jp |
| 高尾鈴栞 | 高尾鈴栞 | `4830079774` | suzuka.takao@gastroduce-japan.co.jp |
| 若松友貴 | 若松友貴 | `0891439285` | tomoki.wakamatsu@gastroduce-japan.co.jp |
| 秋田陽平 | 秋田陽平 | `7481392221` | yohei.akita@gastroduce-japan.co.jp |
| 緒方若菜 | 緒方若菜 | `7374968795` | wakana.ogata@gastroduce-japan.co.jp |
| 上間はるな | 上間はるな | `1055952877` | haruna.uema@gastroduce-japan.co.jp |
| 秦 幸生 | Kosei HATA *(matched by email)* | `5255338606` | kosei.hata@gastroduce-japan.co.jp |
| 西田伊織 | 西田 伊織 | `3850854811` | iori.nishida@gastroduce-japan.co.jp |
| 倉田翼 | 倉田翼 | `8067951632` | tsubasa.kurata@gastroduce-japan.co.jp |
| 草壁 匠 | 草壁　匠 | `0587048116` | sho.kusakabe@gastroduce-japan.co.jp |
| 若松拓弥 | 若松拓弥 | `3538221127` | takuya.wakamatsu@gastroduce-japan.co.jp |
| 渡邉大輔 | 渡邉大輔 | `5035854826` | daisuke.watanabe@gastroduce-japan.co.jp |
| 福元ゆあ | 福元ゆあ | `7187185315` | yua.fukumoto@gastroduce-japan.co.jp |
| 堀江隆文 | 堀江 隆文 | `8098943478` | takafumi.horie@gastroduce-japan.co.jp |

Note: Itsuki and 秦-san match by email because their YAML `name_ja` doesn't
match what NotePM shows them as. The match-by-email path is exactly what
`config/notepm_managers.yaml` is designed for.

## 6. Switching fixed → rolling 6-month window later

The current implementation freezes the cutoff. To make it rolling:

1. Add a Cloud Scheduler job (PRD §4.2.1 already has one for drift reconciliation — co-locate):
   ```sql
   UPDATE documents
      SET deleted_at = now()
    WHERE source = 'notepm'
      AND updated_at < now() - interval '6 months'
      AND deleted_at IS NULL;
   ```
2. Confirm retrieval queries already filter out `deleted_at IS NOT NULL`
   (they do as of 2026-05-25 for drift reconciliation; reuse the same gate).
3. No schema change. No re-backfill.

One-time effect at cutover: pages older than 6 months at that moment all
disappear in one sweep, which is the intent.

## 7. Running the dry-run locally

The `gb-notepm-dryrun` script reads `NOTEPM_API_TOKEN` from env (or `.env`).
For local dev, mint a token on-demand via impersonation — no need to store
it in `.env`. Production reads it from the Secret Manager-mounted env var
on Cloud Run.

```bash
export NOTEPM_API_TOKEN=$(gcloud secrets versions access latest \
  --secret=notepm-api-token \
  --project=31689286230 \
  --impersonate-service-account=gastrobrain-deploy-986@gastrobrain-production.iam.gserviceaccount.com)

# Full scan (cutoff-bounded; ~3-5 min):
.venv/bin/python -m gastrobrain.notepm_cli

# Quick check (first N pages):
.venv/bin/python -m gastrobrain.notepm_cli --max-scan 200

# Override cutoff:
.venv/bin/python -m gastrobrain.notepm_cli --cutoff 2025-08-01

# Larger sample table:
.venv/bin/python -m gastrobrain.notepm_cli --sample 25
```

If `gb-notepm-dryrun` is preferred over `python -m`, run `uv sync` (or the
equivalent for whatever venv manager is on PATH). The user `itsukison` has
`uv` at `/Users/itsukison/.local/bin/uv`; the `gastroduce` account doesn't —
symlink if needed.

## 8. Running the real backfill (`gb-notepm-ingest`)

**Cost-projection basis (verified Cohere pricing):**
- Cohere embed-multilingual-v3.0 is **$0.10/1M input tokens** (verified
  2026-05-25; public pricing page hides the number, confirm from the logged-in
  dashboard).
- Empirical: the 5,608 docs already ingested produced 75,084 chunks
  (~13.4 chunks/doc median). Bound from above with ~250 tokens/chunk →
  the completed work consumed ~19M tokens ≈ $1.90.
- Remaining ~12-month window (~5-6k pages) projects to roughly the same:
  **$2-$3 of additional embed spend**.
- Rerank is retrieval-side; backfill incurs no rerank spend.

**Safeties before running:**
1. Cohere dashboard Spending Limit at `dashboard.cohere.com → Billing & Usage`.
2. `--token-budget` (default 25M tokens ≈ $2.50) hard-stops locally between
   pages before exceeding. Raise via flag if the remaining window needs more.

**Token export:**

```bash
export NOTEPM_API_TOKEN=$(gcloud secrets versions access latest \
  --secret=notepm-api-token --project=31689286230 \
  --impersonate-service-account=gastrobrain-deploy-986@gastrobrain-production.iam.gserviceaccount.com)
```

**Idempotency note:** the CLI scans newest-first and skips pages whose
`content_hash` already matches. So re-running against the existing 5,608 docs
is a no-op until the scan reaches `2025-06-01` (the boundary with the
un-ingested older range), then it starts adding new docs. No special
"resume" mode needed.

**Finish the remaining ~12-month window:**

```bash
# Long-running (~30-60 min projected). Wrap in caffeinate so the laptop
# doesn't suspend mid-batch.
caffeinate -i -s uv run gb-notepm-ingest --token-budget 40000000
```

`caffeinate -i -s`:
- `-i` prevents idle sleep
- `-s` prevents system sleep on AC power (drop if running on battery)
- Process exits when the CLI exits — no need to kill it manually.

**Smoke test option** (recommended first if anything in the pipeline changed):

```bash
caffeinate -i uv run gb-notepm-ingest --limit 20
```

**What gets written:**
- Each matched page → one row in `documents` with `source='notepm'`,
  `external_id=page_code`, `url=https://gastroduce-jp.notepm.jp/page/<page_code>`,
  `author=created_by.name`, `folder_path=[note_name]`, `updated_at`,
  `raw_markdown=body`, `content_hash` (sha256 of body for skip-if-unchanged).
- Each chunk → one row in `chunks` with the `タイトル: <title>\n\n<chunk>` prefix
  (so both lexical and dense indexes can hit on the title), embedding, ordinal,
  heading_path, kind='page'.

**Expected wall-clock for the remaining run:**
- ~12k page list-calls @ 50/min rate limit → ~4 minutes of listing (most are
  skipped by author/cutoff filter without embed cost).
- ~5-6k matched pages × ~13 chunks ≈ 65-80k chunks ≈ 700-900 Cohere batches
  → ~10-20 minutes embedding.
- DB inserts negligible.
- **Total: 30-45 minutes** is the realistic upper bound.

## 9. Not in this PR (deferred)

These are documented in PRD §4.2 and §10.1 — not part of this work:
- Webhook handler at `/notepm/webhook` for incremental sync
- Drift reconciliation cron (every 30 min, soft-delete missing pages)
- Google Drive meeting-transcript ingestion (separate auth track entirely;
  not addressed by the NotePM Secret Manager grant)
