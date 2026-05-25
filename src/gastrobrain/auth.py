"""Supabase JWT verification for the web API.

Supabase issues user access tokens signed either with the legacy HS256 shared
secret (`SUPABASE_JWT_SECRET`) or, on projects created from late 2025 onward,
with an asymmetric key (ES256 / RS256) exposed via the project's JWKS
endpoint. We pick the algorithm from the JWT header and verify accordingly.

RLS still enforces row ownership on the DB side — this dependency only
authenticates the request."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import UUID

import httpx
from fastapi import Header, HTTPException, status
from jose import JWTError, jwt

from gastrobrain.config import get_settings

log = logging.getLogger("gastrobrain.auth")

_JWKS_TTL_SECONDS = 3600
_jwks_cache: dict[str, Any] = {"keys": None, "fetched_at": 0.0}
_jwks_lock = Lock()


@dataclass(frozen=True)
class AuthUser:
    user_id: UUID
    email: str | None


def _jwks_url() -> str | None:
    # .strip() — not just rstrip("/") — because Secret Manager values are
    # frequently set via `echo "…" | gcloud secrets versions add` which
    # appends a trailing newline. A '\n' in the URL makes httpx raise
    # InvalidURL (which does NOT inherit from HTTPError, so the caller's
    # except-clause must catch broadly).
    base = get_settings().supabase_project_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/auth/v1/.well-known/jwks.json"


def _fetch_jwks(force: bool) -> list[dict[str, Any]] | None:
    url = _jwks_url()
    if not url:
        return None
    now = time.time()
    with _jwks_lock:
        cached = _jwks_cache["keys"]
        fresh = (now - float(_jwks_cache["fetched_at"])) < _JWKS_TTL_SECONDS
        if cached and fresh and not force:
            return cached  # type: ignore[no-any-return]
        try:
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            keys = resp.json().get("keys") or []
        except Exception as exc:
            # Broad on purpose: httpx.InvalidURL does not subclass HTTPError
            # in httpx>=0.27, and we never want a JWKS fetch failure to 500
            # the whole API — fall back to cached keys (if any) and let the
            # caller surface a 401.
            log.warning("failed to fetch JWKS from %s: %s", url, exc)
            return cached  # type: ignore[no-any-return]
        _jwks_cache["keys"] = keys
        _jwks_cache["fetched_at"] = now
        return keys


def _pick_jwk(keys: list[dict[str, Any]], kid: str | None) -> dict[str, Any] | None:
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return k
        return None
    return keys[0] if len(keys) == 1 else None


def _decode_asymmetric(token: str, alg: str, kid: str | None) -> dict[str, Any]:
    # Try with the cached JWKS first; on failure refresh once (handles key rotation).
    last_exc: JWTError | None = None
    for force_refresh in (False, True):
        keys = _fetch_jwks(force=force_refresh)
        if not keys:
            raise JWTError("JWKS unavailable")
        jwk = _pick_jwk(keys, kid)
        if jwk is None:
            last_exc = JWTError(f"no matching JWKS key for kid={kid!r}")
            continue
        try:
            return jwt.decode(
                token,
                jwk,
                algorithms=[alg],
                audience="authenticated",
                options={"require_sub": True, "require_exp": True},
            )
        except JWTError as exc:
            last_exc = exc
            continue
    raise last_exc or JWTError("JWKS verification failed")


def _decode_hs256(token: str) -> dict[str, Any]:
    secret = get_settings().supabase_jwt_secret.strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="web API auth is not configured (SUPABASE_JWT_SECRET missing)",
        )
    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience="authenticated",
        options={"require_sub": True, "require_exp": True},
    )


async def require_user(authorization: str | None = Header(default=None)) -> AuthUser:
    """FastAPI dependency. Raises 401 if the JWT is missing, malformed, or invalid."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()

    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        log.info("rejected JWT (bad header): %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    alg = header.get("alg") or "HS256"
    kid = header.get("kid")

    try:
        if alg == "HS256":
            claims = _decode_hs256(token)
        else:
            claims = _decode_asymmetric(token, alg, kid)
    except JWTError as exc:
        log.info("rejected JWT (alg=%s kid=%s): %s", alg, kid, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    sub = claims.get("sub")
    try:
        user_id = UUID(sub)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid sub claim",
        ) from exc

    return AuthUser(user_id=user_id, email=claims.get("email"))


# --------------------------------------------------------------------------------------
# Service tokens (for the MCP transport)
# --------------------------------------------------------------------------------------
#
# MCP clients (Claude Code, Cursor, Claude Desktop, claude.ai connectors) don't
# carry Supabase JWTs. For the read-only `/mcp` route we accept a long-lived
# bearer token instead. Tokens are configured via the GASTROBRAIN_MCP_TOKENS
# env var as a comma-separated list of `label:secret` pairs — labels become
# the `user_id` we record in the `queries` table for telemetry.


def _parse_service_tokens(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        label, secret = entry.split(":", 1)
        label, secret = label.strip(), secret.strip()
        if label and secret:
            out.append((label, secret))
    return out


def verify_service_token(raw_token: str) -> str:
    """Constant-time match `raw_token` against the configured service tokens.

    Three sources, checked cheapest-first:
      1. Env var GASTROBRAIN_MCP_TOKENS — `label:secret` pairs from Secret
         Manager. Admin-minted, break-glass.
      2. `public.mcp_tokens` table — self-service Personal Access Tokens
         minted by logged-in web users. Stored as sha256(token).
      3. OAuth 2.1 access token (JWT) — issued by /oauth/token. Validated
         by HMAC signature + iss/aud/exp without touching the DB.

    Returns the token's label on success. Raises ValueError on miss."""
    raw_bytes = raw_token.encode()
    pairs = _parse_service_tokens(get_settings().gastrobrain_mcp_tokens)
    for label, secret in pairs:
        if hmac.compare_digest(raw_bytes, secret.encode()):
            return label

    # Lazy import to avoid a circular dep (oauth.py imports require_user from
    # this module).
    from gastrobrain.oauth import verify_access_token

    oauth_label = verify_access_token(raw_token)
    if oauth_label is not None:
        return oauth_label

    label = _lookup_db_token(raw_token)
    if label is not None:
        return label

    raise ValueError("token did not match any configured or stored MCP token")


def _lookup_db_token(raw_token: str) -> str | None:
    """Look up a token by sha256 hash in `mcp_tokens`. Returns the label, or
    None on miss. On hit, fire-and-forget update `last_used_at`. Import-local
    to avoid a circular dep — db imports config which is imported here."""
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    from gastrobrain.db import conn

    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE mcp_tokens
                SET last_used_at = now()
                WHERE token_hash = %s AND revoked_at IS NULL
                RETURNING label
                """,
                (digest,),
            )
            row = cur.fetchone()
            c.commit()
            return row[0] if row else None
    except Exception:
        log.exception("mcp_tokens lookup failed")
        return None
