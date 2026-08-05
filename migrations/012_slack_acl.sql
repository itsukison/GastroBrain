-- 012_slack_acl.sql — per-channel access control for source='slack' documents,
-- mirroring the NotePM-derived model in 011_notepm_acl.sql.
--
-- Until now every Slack document was unrestricted (the retrieve.py gate only
-- filtered source='notepm'). This adds Slack's own access semantics to the
-- corpus: a person sees a Slack document iff its channel is PUBLIC (anyone in
-- the workspace can read it) OR they are a member of that PRIVATE channel.
-- Channel membership is mirrored nightly from the Slack API (gb-slack-acl-sync)
-- into flat (channel_id, slack_user_id) rows so the runtime gate is a single
-- indexed lookup, exactly like notepm_note_access.
--
-- Identity is keyed by slack_user_id. The sync stamps members.slack_user_id by
-- email match (Slack profile email ↔ members.email), so web/MCP callers — not
-- just the Slack bot — resolve to their channel access too.
--
-- This migration only ADDS structures; the gate stays NotePM-only until the
-- app-code change lands. Existing Slack docs keep a null slack_channel_id until
-- backfilled (see below), so once the gate is live they are hidden until the
-- first acl-sync + backfill — fail-closed, never accidentally open.

BEGIN;

-- The channels we mirror access for, with their private flag. Public channels
-- (is_private = false) are visible to every signed-in user; private channels
-- are gated by slack_channel_access. Populated by gb-slack-acl-sync.
CREATE TABLE slack_channels (
    channel_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    is_private  BOOLEAN NOT NULL DEFAULT false,
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Resolved per-channel access: one row per (private channel, member). Public
-- channels get NO rows here — the gate treats them as open via is_private.
CREATE TABLE slack_channel_access (
    channel_id     TEXT NOT NULL REFERENCES slack_channels (channel_id) ON DELETE CASCADE,
    slack_user_id  TEXT NOT NULL,
    PRIMARY KEY (channel_id, slack_user_id)
);

-- Runtime lookup is "which channels can THIS slack user see" → lead with the user.
CREATE INDEX slack_channel_access_user_idx ON slack_channel_access (slack_user_id, channel_id);

-- Which channel a document belongs to. Stamped at ingest; null for non-Slack docs.
ALTER TABLE documents ADD COLUMN slack_channel_id TEXT;
CREATE INDEX documents_slack_channel_id_idx ON documents (slack_channel_id) WHERE deleted_at IS NULL;

-- Backfill the existing Slack docs from external_id (`<channel_id>:<YYYY-MM-DD>`).
UPDATE documents
   SET slack_channel_id = split_part(external_id, ':', 1)
 WHERE source = 'slack' AND slack_channel_id IS NULL;

-- RLS — defence-in-depth, same pattern as 011: service-role-only, no permissive
-- policy. The FastAPI pool bypasses RLS; any anon/authenticated access is denied.
ALTER TABLE slack_channels       ENABLE ROW LEVEL SECURITY;
ALTER TABLE slack_channel_access ENABLE ROW LEVEL SECURITY;

COMMIT;
