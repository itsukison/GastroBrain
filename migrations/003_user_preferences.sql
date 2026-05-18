-- 003_user_preferences.sql — per-user web settings.
--
-- v1 scope: a single field (`department`) that gets appended to the Sonnet
-- system prompt as supplementary context so answers can lean on department-
-- specific vocabulary. Coreルール (citation, refusal, injection defence) は
-- generate._BASE_RULES に固定。ここの設定が上書きすることはない。
--
-- Slack側はこの設定を読まない（surface='web' のときだけ適用）。
--
-- Row is created lazily on first PUT — absence is treated as "未設定".

BEGIN;

CREATE TABLE user_preferences (
    user_id     UUID PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
    department  TEXT CHECK (
        department IN ('consulting', 'sales', 'content', 'dev', 'backoffice', 'other')
    ),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS — defence-in-depth. FastAPI uses the service-role pool (RLS bypassed),
-- and enforces `WHERE user_id = $1` explicitly. These policies cover any
-- future direct PostgREST/anon-key access.
ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_prefs_owner_select
    ON user_preferences FOR SELECT
    USING (user_id = auth.uid());

CREATE POLICY user_prefs_owner_modify
    ON user_preferences FOR ALL
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

COMMIT;
