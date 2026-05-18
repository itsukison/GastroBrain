-- 002_web_chat.sql — persistent conversations + messages for the web surface.
--
-- The Slack bot is one-shot: each question writes to `queries` and that's the
-- whole story. The web app needs multi-turn threads with a sidebar, so we
-- introduce `conversations` (one row per chat) and `messages` (one row per
-- user or assistant turn).
--
-- `messages.query_id` joins to the existing `queries` table so eval / cost /
-- feedback continue to read from a single source. We deliberately do NOT
-- duplicate the question/answer text out of `messages` into `queries` — the
-- Slack path writes only to `queries`, the web path writes to both.

BEGIN;

CREATE TABLE conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    title         TEXT NOT NULL DEFAULT '新規チャット',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at   TIMESTAMPTZ,
    deleted_at    TIMESTAMPTZ
);

-- One index serves both the sidebar listing (active threads, newest first)
-- and the archive view (we'll filter on archived_at IS NOT NULL when needed).
CREATE INDEX conversations_user_active_updated
    ON conversations (user_id, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE messages (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  UUID NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    -- Assistant-only: the chunk UUIDs cited in this answer.
    cited_chunks     UUID[],
    -- Assistant-only: links to the `queries` row that holds tokens/cost/feedback.
    query_id         UUID REFERENCES queries (id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX messages_conversation_created
    ON messages (conversation_id, created_at);

-- Touch the parent conversation's updated_at whenever a message lands.
-- Sidebar ordering is "most recently active first" — keep it cheap.
CREATE OR REPLACE FUNCTION touch_conversation_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    UPDATE conversations
       SET updated_at = now()
     WHERE id = NEW.conversation_id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER messages_touch_conversation
AFTER INSERT ON messages
FOR EACH ROW EXECUTE FUNCTION touch_conversation_updated_at();

-- --------------------------------------------------------------------------
-- Row-level security
-- --------------------------------------------------------------------------
-- The web API service authenticates the user via Supabase JWT, then runs SQL
-- through Supabase Postgres with the user's role propagated. RLS makes
-- ownership the table's invariant rather than the app's promise.

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages       ENABLE ROW LEVEL SECURITY;

CREATE POLICY conv_owner_select
    ON conversations FOR SELECT
    USING (user_id = auth.uid() AND deleted_at IS NULL);

CREATE POLICY conv_owner_modify
    ON conversations FOR ALL
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

CREATE POLICY msg_owner_select
    ON messages FOR SELECT
    USING (conversation_id IN (
        SELECT id FROM conversations
         WHERE user_id = auth.uid() AND deleted_at IS NULL
    ));

CREATE POLICY msg_owner_modify
    ON messages FOR ALL
    USING (conversation_id IN (
        SELECT id FROM conversations WHERE user_id = auth.uid()
    ))
    WITH CHECK (conversation_id IN (
        SELECT id FROM conversations WHERE user_id = auth.uid()
    ));

COMMIT;
