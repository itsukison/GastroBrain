# Access control — NotePM-derived corpus visibility

**Last updated:** 2026-06-11 (shipped to prod)

How Gastrobrain decides which documents a given person may retrieve, across
every surface (web chat, Slack bot, MCP). NotePM is the **source of truth** —
there is no manual permission step.

## 1. Model

Access is **set-based and per-notebook**, mirroring NotePM's own model — *not* a
hierarchical clearance ladder (that was the original design in
`migrations/006_rbac.sql`, now superseded).

A person may see a NotePM document iff **any** of these holds:

1. The document is **not** from NotePM (`source` ∈ `slack` / `gdrive` /
   `manual`) — these are unrestricted and visible to every signed-in user.
2. The document's notebook is **public** (NotePM `scope = open`).
3. The caller's **NotePM `user_code`** is in that notebook's resolved access set
   = explicit per-user grants **∪** members of any group on the notebook.

Identity is keyed by **email** across all surfaces. An email with no matching
NotePM account resolves to **public-only** (fail-closed — never silently open).

> NotePM permissions are at the **notebook (note)** level. Our corpus is already
> ingested at notebook granularity (`documents.note_code`), so the boundary lines
> up exactly. Folder/page-level ACLs are not exposed by the NotePM API and are
> not used.

### Why not the clearance ladder?

NotePM grants are arbitrary principal *sets*: a notebook can be visible to
`010_コンサル` **and** `036_システム開発` **and** two named individuals at once.
Departments like 新規営業課 vs システム開発部 are disjoint — neither is "higher"
than the other, so they cannot collapse into a single 1–4 `min_level` integer.
The set-based model represents this faithfully.

## 2. Runtime gate

`gastrobrain.access.AccessScope` is the per-request visibility object:

| Scope | Meaning |
|---|---|
| `AccessScope(user_code=<code>)` | A NotePM identity — gated by note membership. |
| `PUBLIC_ONLY` (`user_code=None`) | No NotePM account → public notes + non-NotePM docs only. **Default / fail-closed.** |
| `SEE_ALL` (`see_all=True`) | Break-glass — sees everything. |

Resolution per surface (all in `gastrobrain.access`):

- **Web** (`web_api.py` chat): `email → members.notepm_user_code` → `AccessScope`.
- **Slack** (`slack_app.py`): `scope_by_slack_id()` (cached `members.slack_user_id`),
  falling back to `users.info` email lookup + `link_slack_id()`.
- **MCP** (`auth.py` / `mcp_server.py`): OAuth/PAT tokens resolve by email; env-var
  service tokens (`GASTROBRAIN_MCP_TOKENS`) get `SEE_ALL` (operator break-glass).
- **CLIs** (`gb-ask`, `gb-chat`): `SEE_ALL` (local operator tools).

The gate itself is one SQL clause in `retrieve.py` (`_ACCESS_CLAUSE`), applied to
both the dense and lexical arms so restricted docs never enter the candidate set
— they can't be retrieved, reranked, or cited.

Admins (`members.is_admin`) are **not** special here — `is_admin` only governs the
org/settings UI, never retrieval. Admins see exactly their own NotePM scope.

## 3. Schema (`migrations/011_notepm_acl.sql`)

| Object | Purpose |
|---|---|
| `members.notepm_user_code` | Email-matched NotePM identity (stamped by the sync). |
| `notepm_notes (note_code PK, name, is_public, synced_at)` | Mirrored notebooks + public flag. |
| `notepm_note_access (note_code, user_code)` | Resolved access set — group memberships expanded into flat rows so the gate is a single indexed lookup. |
| `documents.note_code` | Which notebook a doc belongs to (null for non-NotePM sources). |

The legacy ladder tables (`roles`, `folder_acl`, `documents.min_level`) are left
in place but **dormant** for rollback safety; a later PR drops them.

## 4. Sync (`gb-notepm-acl-sync`)

`gastrobrain.notepm_acl.sync_acl()` is a full-refresh, idempotent rebuild from the
live NotePM API (`gastrobrain.notepm`: `list_notes_acl` / `list_groups` /
`group_members`). One transaction:

1. Upsert `notepm_notes`; drop notebooks that vanished (cascades access rows).
2. Rebuild `notepm_note_access` = per note, explicit users ∪ each group's members.
3. Re-derive every `members.notepm_user_code` by email, **upserting a member row
   for every NotePM user** — this is what makes a new Slack/web sign-in resolve to
   their access with zero admin action.

Cost is ~30 API calls (1 users + 1 notes + ~23 groups), trivial at the 50 req/min
bucket.

```bash
# Dry-run (no writes):
NOTEPM_API_TOKEN=… NOTEPM_TEAM_SUBDOMAIN=gastroduce-jp uv run gb-notepm-acl-sync --dry-run
# Real:
… uv run gb-notepm-acl-sync
```

**Production:** runs nightly as the second step of Cloud Run job
`gastrobrain-notepm-sync` — `gb-notepm-ingest && gb-notepm-acl-sync` at 03:00 JST
(so the ACL sync sees freshly-ingested `note_code` values). See
`deploy/notepm_sync_job.sh`.

State as of 2026-06-11: 53 notebooks (2 public), 1,301 access rows, 63 NotePM
users linked, 9,569 NotePM documents.

## 5. Self-service "accessible documents" view

`GET /v1/org/me/access` (any signed-in user) returns the notebooks the caller can
see with per-notebook doc counts. The web `/org` page renders this read-only as
**「アクセスできる資料」** (`web/src/components/org-view.tsx`). There is nothing to
edit — permissions are changed in NotePM and flow in on the next sync.

## 6. Caveats

- **Email must match.** If a person's NotePM email differs from their Google
  login email, they won't link and fall back to public-only until reconciled.
- **Up to 24h lag.** A new NotePM user or a freshly-granted notebook only appears
  after the next nightly sync. Run `gb-notepm-acl-sync` manually to apply sooner.
- **Not in NotePM at all** (e.g. a Slack-only contractor) → public-only.

## 7. Debugging a user's access

```sql
-- What can this email see?
SELECT n.name, n.is_public, count(d.id) AS docs
FROM notepm_notes n
JOIN documents d ON d.note_code = n.note_code
                 AND d.source='notepm' AND d.deleted_at IS NULL
WHERE n.is_public
   OR n.note_code IN (
        SELECT note_code FROM notepm_note_access
        WHERE user_code = (SELECT notepm_user_code FROM members WHERE email = lower('user@…'))
      )
GROUP BY n.name, n.is_public ORDER BY n.name;
```

A common "I can't see X" cause is a missing/mismatched `members.notepm_user_code`
(`SELECT email, notepm_user_code FROM members WHERE email = …`).
