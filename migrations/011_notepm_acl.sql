-- 011_notepm_acl.sql — replace the manual clearance-ladder ACL with NotePM-derived,
-- set-based access. NotePM is the source of truth: a person sees a NotePM note iff
-- their NotePM user_code is in that note's resolved access set (explicit users ∪
-- members of any group on the note), or the note is public (scope='open'). The set
-- is synced nightly from the NotePM API (groups expanded into flat rows here so the
-- runtime gate is a single membership lookup).
--
-- Scope: this governs source='notepm' documents only. slack / gdrive / manual docs
-- have a null note_code and are unaffected — they stay visible to everyone, exactly
-- as today (folder_acl has 0 rows in prod, so nothing is currently restricted).
--
-- This migration only ADDS structures; they are dormant until the app-code gate
-- swap lands. The 1–4 ladder (roles / members.role_id / folder_acl / min_level) is
-- intentionally left in place for rollback safety — a later PR drops it once the
-- NotePM-derived path is proven in prod.

BEGIN;

-- ---------------------------------------------------------------------------
-- email → NotePM identity. Stamped by the nightly sync via email match
-- (members.email ↔ NotePM /users email, lowercased). Null = no NotePM account
-- matched → that person sees only public notes (fail-closed) once the gate is live.
-- ---------------------------------------------------------------------------

ALTER TABLE members ADD COLUMN notepm_user_code TEXT;

-- ---------------------------------------------------------------------------
-- The notebooks we mirror access for, with their public flag.
-- is_public mirrors NotePM scope='open' (visible to the whole team).
-- ---------------------------------------------------------------------------

CREATE TABLE notepm_notes (
    note_code   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    is_public   BOOLEAN NOT NULL DEFAULT false,
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Resolved per-note access: one row per (note, user_code) allowed to view it.
-- Group memberships are expanded into rows by the sync, so the gate stays flat.
CREATE TABLE notepm_note_access (
    note_code   TEXT NOT NULL REFERENCES notepm_notes (note_code) ON DELETE CASCADE,
    user_code   TEXT NOT NULL,
    PRIMARY KEY (note_code, user_code)
);

-- Runtime lookup is "which notes can THIS user_code see" → lead with user_code.
CREATE INDEX notepm_note_access_user_idx ON notepm_note_access (user_code, note_code);

-- ---------------------------------------------------------------------------
-- Which note a document belongs to. Populated at ingest (page.note_code) and
-- backfilled for existing rows. Null for non-NotePM sources.
-- ---------------------------------------------------------------------------

ALTER TABLE documents ADD COLUMN note_code TEXT;
CREATE INDEX documents_note_code_idx ON documents (note_code) WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- RLS — defence-in-depth, same pattern as 006_rbac.sql: enable with no
-- permissive policy so these tables are service-role-only. The FastAPI pool
-- bypasses RLS; any future anon/authenticated PostgREST access is denied.
-- ---------------------------------------------------------------------------

ALTER TABLE notepm_notes        ENABLE ROW LEVEL SECURITY;
ALTER TABLE notepm_note_access  ENABLE ROW LEVEL SECURITY;

COMMIT;
