-- 005_oauth.sql — OAuth 2.1 authorization server for the /mcp/ endpoint.
--
-- We turn Gastrobrain into a real OAuth AS so MCP clients (Claude Code,
-- Cursor, Claude Desktop, claude.ai connectors) can register dynamically,
-- pop a browser-based Google login, and obtain tokens without the user ever
-- copy-pasting a bearer. The MCP spec (2025-03-26 rev) mandates OAuth 2.1
-- with mandatory PKCE for remote servers.
--
-- Three tables:
--   - oauth_clients              — registered MCP clients (one row per
--                                  Claude Code install, etc.). All public,
--                                  no client_secret.
--   - oauth_authorization_codes  — short-lived (5 min) one-time-use codes.
--   - oauth_refresh_tokens       — long-lived (30 day), opaque, rotated.
--
-- Access tokens are NOT stored — they're stateless JWTs signed by
-- GASTROBRAIN_OAUTH_JWT_KEY with a 1h TTL. Revocation happens at the refresh
-- layer (kill the refresh token, the access token expires within 1h).

BEGIN;

CREATE TABLE oauth_clients (
    client_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_name    TEXT,
    redirect_uris  TEXT[] NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_authorization_codes (
    code                  TEXT PRIMARY KEY,
    client_id             UUID NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    user_id               UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    redirect_uri          TEXT NOT NULL,
    -- PKCE: we only accept S256. `code_challenge` is base64url(sha256(verifier)).
    code_challenge        TEXT NOT NULL,
    scope                 TEXT,
    expires_at            TIMESTAMPTZ NOT NULL,
    -- Single-use. Marking used_at lets us detect replay rather than silently
    -- succeeding twice; once a code is used we revoke any tokens issued from
    -- a duplicate-use attempt.
    used_at               TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX oauth_codes_active_idx
    ON oauth_authorization_codes (expires_at)
    WHERE used_at IS NULL;

CREATE TABLE oauth_refresh_tokens (
    -- Raw refresh tokens never persist — we store only sha256(token).
    token_hash         TEXT PRIMARY KEY,
    client_id          UUID NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    user_id            UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    -- Rotation chain: a refresh token's parent is the previous token in the
    -- rotation. Reuse of an already-rotated token triggers chain revocation.
    parent_token_hash  TEXT REFERENCES oauth_refresh_tokens(token_hash) ON DELETE SET NULL,
    scope              TEXT,
    expires_at         TIMESTAMPTZ NOT NULL,
    revoked_at         TIMESTAMPTZ,
    last_used_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX oauth_refresh_user_active_idx
    ON oauth_refresh_tokens (user_id)
    WHERE revoked_at IS NULL;

-- RLS — these tables are only accessed by FastAPI via the service-role pool
-- (RLS bypassed) which enforces ownership via WHERE user_id = $1 explicitly.
-- These policies are defense-in-depth for any future PostgREST/anon access.
ALTER TABLE oauth_clients               ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_authorization_codes   ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_refresh_tokens        ENABLE ROW LEVEL SECURITY;

-- oauth_clients has no user_id (clients are shared across users) — only the
-- service role should touch it. No policies = no anon access.

CREATE POLICY oauth_codes_owner
    ON oauth_authorization_codes FOR ALL
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

CREATE POLICY oauth_refresh_owner
    ON oauth_refresh_tokens FOR ALL
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

COMMIT;
