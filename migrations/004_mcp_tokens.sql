-- 004_mcp_tokens.sql — self-service MCP tokens per Supabase user.
--
-- Before this, only the GCP-secret-resident GASTROBRAIN_MCP_TOKENS env var
-- was consulted by `verify_service_token`, so every new teammate had to ping
-- an admin to mint a token. This table lets logged-in web users mint their
-- own from the settings modal.
--
-- Storage: we never persist the raw token. The /v1/mcp/tokens POST handler
-- mints a `tok_<urlsafe-32>` value, returns it once, and stores only
-- sha256(token) here. /mcp/ auth re-hashes the incoming bearer and looks it
-- up by hash. Env-var tokens still work as a break-glass / admin path.

BEGIN;

CREATE TABLE mcp_tokens (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    -- hex sha256 of the raw token. Unique partial index below covers active rows.
    token_hash    TEXT NOT NULL,
    -- Becomes `mcp:<label>` in the queries table. Derived from email prefix
    -- at mint time so traffic is identifiable per user in dashboards.
    label         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ
);

-- Hash lookups happen on every /mcp/ call. Restrict the unique constraint to
-- live tokens so a revoked token can't poison a future mint that happens to
-- collide (vanishingly unlikely with 32 random bytes, but cheap to guard).
CREATE UNIQUE INDEX mcp_tokens_hash_active_uidx
    ON mcp_tokens (token_hash)
    WHERE revoked_at IS NULL;

CREATE INDEX mcp_tokens_user_active_idx
    ON mcp_tokens (user_id)
    WHERE revoked_at IS NULL;

-- RLS — defence-in-depth. FastAPI uses the service-role pool (RLS bypassed)
-- and enforces `WHERE user_id = $1` explicitly, but these cover any future
-- direct PostgREST/anon-key access.
ALTER TABLE mcp_tokens ENABLE ROW LEVEL SECURITY;

CREATE POLICY mcp_tokens_owner_select
    ON mcp_tokens FOR SELECT
    USING (user_id = auth.uid());

CREATE POLICY mcp_tokens_owner_modify
    ON mcp_tokens FOR ALL
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

COMMIT;
