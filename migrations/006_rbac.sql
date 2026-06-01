-- 006_rbac.sql — role-based access control over the corpus.
--
-- Model: hierarchical clearance LEVELS, not departments. Each person holds at
-- most one role; a higher level sees everything the lower levels see plus more.
-- A NotePM folder can require a minimum level to view; folders with no rule are
-- visible to everyone (level 0). Department (003) is untouched — it stays a
-- self-selected personalisation hint, NOT a security boundary.
--
-- Identity across surfaces is keyed by EMAIL (lowercased @gastroduce-japan.co.jp),
-- because web (Google), MCP (OAuth/PAT email claim) and Slack (users.info email)
-- all resolve to it. `members.slack_user_id` caches the Slack→email mapping so
-- we don't hit the Slack API on every question.
--
-- Enforcement is one indexed column: `documents.min_level`, kept in sync from
-- `folder_acl` by a trigger (covers EVERY ingest path — manual, NotePM webhook,
-- drift reconcile — with zero app-code changes) and a bulk recompute the org
-- API calls after editing rules. Retrieval just adds `AND d.min_level <= $level`.

BEGIN;

-- ---------------------------------------------------------------------------
-- Clearance ladder
-- ---------------------------------------------------------------------------

CREATE TABLE roles (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    -- The clearance level. Higher = more access. Level 0 is implicit
    -- ("everyone") and is never stored as a row.
    level       INT  NOT NULL UNIQUE CHECK (level >= 1),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO roles (name, level) VALUES
    ('メンバー',       1),
    ('リーダー',       2),
    ('マネージャー',   3),
    ('役員',           4);

-- ---------------------------------------------------------------------------
-- People → role. Keyed by email so we can pre-provision someone (e.g. a
-- Slack-only user) before they ever log into the web app.
-- ---------------------------------------------------------------------------

CREATE TABLE members (
    email          TEXT PRIMARY KEY,                       -- lowercased
    role_id        INT REFERENCES roles (id) ON DELETE SET NULL,
    is_admin       BOOLEAN NOT NULL DEFAULT false,         -- can open the org view
    slack_user_id  TEXT UNIQUE,                            -- cached Slack identity
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bootstrap the first admin (also top clearance). Everyone else starts unmapped
-- (level 0 → sees only unrestricted folders, which today is all of them).
INSERT INTO members (email, role_id, is_admin)
VALUES (
    'itsuki.son@gastroduce-japan.co.jp',
    (SELECT id FROM roles WHERE level = 4),
    true
);

-- ---------------------------------------------------------------------------
-- Folder restrictions. One rule per folder prefix; the rule's min_level is the
-- clearance required to view any document whose folder_path starts with it.
-- ---------------------------------------------------------------------------

CREATE TABLE folder_acl (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- A prefix of documents.folder_path, e.g. {'経営','M&A'}. UNIQUE so a folder
    -- has exactly one rule. The empty array would match every doc; the UI never
    -- creates it, but the prefix logic below handles it correctly if it appears.
    folder_prefix  TEXT[] NOT NULL UNIQUE,
    min_level      INT NOT NULL CHECK (min_level >= 1),
    note           TEXT,
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- documents.min_level — the materialised, indexed enforcement column.
-- ---------------------------------------------------------------------------

ALTER TABLE documents ADD COLUMN min_level INT NOT NULL DEFAULT 0;
CREATE INDEX documents_min_level_idx ON documents (min_level);

-- Required clearance for a given folder_path = the MOST restrictive matching
-- prefix rule (a deeper restricted subfolder can raise, never lower, access).
-- Returns 0 when no rule matches → visible to everyone.
CREATE OR REPLACE FUNCTION compute_min_level(fpath TEXT[]) RETURNS INT
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(MAX(fa.min_level), 0)
    FROM folder_acl fa
    WHERE fpath[1:cardinality(fa.folder_prefix)] = fa.folder_prefix;
$$;

-- Keep documents.min_level correct on every insert / folder move. Fires only
-- when folder_path is touched, so the bulk recompute below (which writes
-- min_level directly) does not re-trigger it.
CREATE OR REPLACE FUNCTION documents_set_min_level() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.min_level := compute_min_level(NEW.folder_path);
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_documents_min_level
    BEFORE INSERT OR UPDATE OF folder_path ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_set_min_level();

-- Re-stamp every document. The org API calls this after any folder_acl change.
CREATE OR REPLACE FUNCTION recompute_document_levels() RETURNS VOID
LANGUAGE sql AS $$
    UPDATE documents SET min_level = compute_min_level(folder_path);
$$;

-- Initial backfill — no rules exist yet, so this stamps every doc to 0 (no
-- behavioural change at deploy; enforcement is dormant until a rule is added).
SELECT recompute_document_levels();

-- ---------------------------------------------------------------------------
-- RLS — defence-in-depth. The FastAPI service-role pool bypasses RLS and gates
-- writes behind an explicit is_admin check in the org endpoints. With RLS on
-- and no permissive policy, any future anon/authenticated PostgREST access is
-- denied by default — these tables are admin-only.
-- ---------------------------------------------------------------------------

ALTER TABLE roles      ENABLE ROW LEVEL SECURITY;
ALTER TABLE members    ENABLE ROW LEVEL SECURITY;
ALTER TABLE folder_acl ENABLE ROW LEVEL SECURITY;

-- A logged-in user may read their own member row (e.g. to learn their level);
-- everything else stays service-role-only.
CREATE POLICY members_self_select
    ON members FOR SELECT
    USING (email = lower(auth.jwt() ->> 'email'));

COMMIT;
