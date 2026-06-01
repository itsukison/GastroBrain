"""OAuth 2.1 authorization server for the /mcp/ endpoint.

Implements just enough of the spec to let MCP clients (Claude Code, Cursor,
Claude Desktop, claude.ai connectors) discover the AS, dynamically register
themselves, run a browser-based Google login, and obtain access tokens —
no copy-pasting bearers.

Endpoints
---------
- GET  /.well-known/oauth-protected-resource   resource metadata (RFC 9728)
- GET  /.well-known/oauth-authorization-server AS metadata       (RFC 8414)
- POST /oauth/register                          dynamic client registration (RFC 7591)
- GET  /oauth/authorize                         user-facing entry; redirects to Google
- GET  /oauth/google-callback                   Google → our AS
- POST /oauth/token                             code & refresh grants

Design notes
------------
- All clients are public (no client_secret). PKCE (S256) is mandatory.
- Access tokens are stateless JWTs (HS256, 1h). Refresh tokens are opaque
  (sha256(raw) stored), 30d, rotated on every use with reuse-detection.
- Identity is Google → email → existing auth.users row. Users who haven't
  signed in to the web app yet get a clear "please visit gastrobrain first"
  error rather than a lazy-created user.
- The `state` parameter that survives the Google round trip is itself a
  signed JWT, so we don't need server-side session storage between the
  /oauth/authorize call and the /oauth/google-callback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from gastrobrain.config import get_settings
from gastrobrain.db import conn

log = logging.getLogger("gastrobrain.oauth")

router = APIRouter(tags=["oauth"])

# Token lifetimes
ACCESS_TOKEN_TTL_SECONDS = 3600                   # 1h
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600        # 30d
AUTH_CODE_TTL_SECONDS = 300                       # 5m
STATE_TTL_SECONDS = 600                           # 10m to complete the Google round trip

# Scopes — single scope for v1. Easy to expand later.
DEFAULT_SCOPE = "mcp:search"
SUPPORTED_SCOPES = ["mcp:search"]

# Google's OAuth endpoints (stable, well-known)
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"

# We trust @gastroduce-japan.co.jp only.
ALLOWED_EMAIL_DOMAIN = "gastroduce-japan.co.jp"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _settings_or_503():
    s = get_settings()
    missing = [
        f
        for f in (
            "google_oauth_client_id",
            "google_oauth_client_secret",
            "gastrobrain_oauth_jwt_key",
            "gastrobrain_oauth_state_key",
            "gastrobrain_oauth_issuer",
        )
        if not getattr(s, f, "").strip()
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"OAuth not configured (missing: {', '.join(m.upper() for m in missing)})",
        )
    return s


def _issuer() -> str:
    return get_settings().gastrobrain_oauth_issuer.rstrip("/")


def _resource_url() -> str:
    return f"{_issuer()}/mcp/"


def _label_from_email(email: str) -> str:
    local = (email or "").split("@", 1)[0].strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]", "", local)
    return cleaned or "user"


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _verify_pkce_s256(verifier: str, expected_challenge: str) -> bool:
    """PKCE S256: challenge = base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, expected_challenge)


def _valid_redirect_uri(uri: str) -> bool:
    """Accept https URLs and http loopback (RFC 8252). MCP CLIs typically
    register http://127.0.0.1:<port>/callback or http://localhost:<port>/cb."""
    if uri.startswith("https://"):
        return True
    if uri.startswith("http://127.0.0.1") or uri.startswith("http://localhost"):
        return True
    return False


def _make_state_jwt(payload: dict[str, Any]) -> str:
    s = _settings_or_503()
    return jwt.encode(
        {**payload, "iat": int(time.time()), "exp": int(time.time()) + STATE_TTL_SECONDS},
        s.gastrobrain_oauth_state_key,
        algorithm="HS256",
    )


def _verify_state_jwt(token: str) -> dict[str, Any]:
    s = _settings_or_503()
    try:
        return jwt.decode(token, s.gastrobrain_oauth_state_key, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=400, detail=f"invalid state: {exc}") from exc


def _mint_access_token(*, user_id: UUID, email: str, client_id: UUID, scope: str) -> str:
    s = _settings_or_503()
    label = _label_from_email(email)
    now = int(time.time())
    return jwt.encode(
        {
            "iss": _issuer(),
            "sub": str(user_id),
            "aud": _resource_url(),
            "iat": now,
            "exp": now + ACCESS_TOKEN_TTL_SECONDS,
            "scope": scope,
            "email": email,
            "label": label,
            "client_id": str(client_id),
            "jti": secrets.token_urlsafe(16),
        },
        s.gastrobrain_oauth_jwt_key,
        algorithm="HS256",
    )


def _decode_access_token(token: str) -> dict | None:
    """Validate an OAuth access token (JWT) and return its claims, or None."""
    s = get_settings()
    if not s.gastrobrain_oauth_jwt_key.strip():
        return None
    try:
        return jwt.decode(
            token,
            s.gastrobrain_oauth_jwt_key,
            algorithms=["HS256"],
            audience=_resource_url(),
            issuer=_issuer(),
            options={"require_sub": True, "require_exp": True, "require_aud": True},
        )
    except JWTError as exc:
        log.info("oauth access token rejected: %s", exc)
        return None


def verify_access_token(token: str) -> str | None:
    """Validate an OAuth access token. Returns the telemetry label, or None."""
    claims = _decode_access_token(token)
    if claims is None:
        return None
    return claims.get("label") or _label_from_email(claims.get("email", ""))


def verify_access_token_identity(token: str) -> tuple[str, str | None] | None:
    """Validate an OAuth access token and return (label, email), or None. The
    email drives clearance-level resolution in auth.verify_service_token."""
    claims = _decode_access_token(token)
    if claims is None:
        return None
    label = claims.get("label") or _label_from_email(claims.get("email", ""))
    return label, claims.get("email")


# --------------------------------------------------------------------------------------
# .well-known endpoints
# --------------------------------------------------------------------------------------


def _prm_payload() -> dict[str, Any]:
    return {
        "resource": _resource_url(),
        "authorization_servers": [_issuer()],
        "scopes_supported": SUPPORTED_SCOPES,
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728 — tells MCP clients which AS protects this resource."""
    return _prm_payload()


@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata_at_path() -> dict[str, Any]:
    """Per-resource variant per RFC 9728 §3.1: for resources whose URL has a
    path component, the metadata URL should reflect that path. claude.ai's
    connector checks this URL first and falls back to the path-less variant
    only on 404 — serving both removes a (harmless but noisy) 404 from the
    flow."""
    return _prm_payload()


@router.get("/.well-known/oauth-authorization-server")
async def auth_server_metadata() -> dict[str, Any]:
    """RFC 8414 — AS endpoint discovery."""
    base = _issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": SUPPORTED_SCOPES,
    }


# --------------------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# --------------------------------------------------------------------------------------


class ClientRegistrationBody(BaseModel):
    client_name: str | None = None
    redirect_uris: list[str] = Field(default_factory=list)
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    token_endpoint_auth_method: str | None = None
    # Any other fields the client sends are ignored.


@router.post("/oauth/register")
async def register_client(body: ClientRegistrationBody) -> JSONResponse:
    _settings_or_503()
    if not body.redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required")
    for uri in body.redirect_uris:
        if not _valid_redirect_uri(uri):
            raise HTTPException(
                status_code=400,
                detail=f"redirect_uri must be https:// or http loopback: {uri}",
            )
    if body.token_endpoint_auth_method and body.token_endpoint_auth_method != "none":
        # We only support public clients — secrets aren't useful for CLIs that
        # ship in user-readable installs anyway.
        raise HTTPException(
            status_code=400,
            detail="only token_endpoint_auth_method='none' is supported (public clients)",
        )

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_clients (client_name, redirect_uris)
                VALUES (%s, %s)
                RETURNING client_id, created_at
                """,
                (body.client_name, body.redirect_uris),
            )
            row = cur.fetchone()
            c.commit()
            return {
                "client_id": str(row[0]),
                "client_id_issued_at": int(row[1].timestamp()),
            }

    import asyncio

    result = await asyncio.to_thread(_do)
    return JSONResponse(
        {
            **result,
            "client_name": body.client_name,
            "redirect_uris": body.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


# --------------------------------------------------------------------------------------
# /oauth/authorize  →  redirect to Google
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _AuthRequest:
    client_id: str
    redirect_uri: str
    code_challenge: str
    scope: str
    original_state: str | None
    nonce: str  # CSRF: must match the one we stored in the state JWT


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
    state: str | None = None,
) -> RedirectResponse:
    s = _settings_or_503()

    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be 'code'")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="code_challenge_method must be 'S256'")
    if not code_challenge:
        raise HTTPException(status_code=400, detail="code_challenge is required")
    if not _valid_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="invalid redirect_uri")

    try:
        client_uuid = UUID(client_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid client_id") from exc

    import asyncio

    def _validate_client() -> list[str]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT redirect_uris FROM oauth_clients WHERE client_id = %s",
                (str(client_uuid),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="unknown client_id")
            return list(row[0] or [])

    registered = await asyncio.to_thread(_validate_client)
    if redirect_uri not in registered:
        # Exact-match per RFC 6749 §3.1.2.4. No prefix/wildcard.
        raise HTTPException(status_code=400, detail="redirect_uri not registered for this client")

    # Reduce requested scope to what we support (silently drop unknown scopes).
    requested = [t for t in (scope or "").split() if t]
    granted = [t for t in requested if t in SUPPORTED_SCOPES] or [DEFAULT_SCOPE]
    granted_scope = " ".join(sorted(set(granted)))

    nonce = secrets.token_urlsafe(16)
    state_jwt = _make_state_jwt(
        {
            "client_id": str(client_uuid),
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": granted_scope,
            "original_state": state,
            "nonce": nonce,
        }
    )

    google_params = {
        "client_id": s.google_oauth_client_id,
        "redirect_uri": f"{_issuer()}/oauth/google-callback",
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state_jwt,
        # hd hint — combined with the consent-screen "Internal" setting,
        # restricts to Workspace users in our domain.
        "hd": ALLOWED_EMAIL_DOMAIN,
    }
    return RedirectResponse(
        url=f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(google_params)}",
        status_code=302,
    )


# --------------------------------------------------------------------------------------
# /oauth/google-callback  →  exchange + lookup + mint our auth code  →  client
# --------------------------------------------------------------------------------------


def _ineligible_html(message: str, return_uri: str | None = None) -> HTMLResponse:
    """Render a minimal error page for cases we can't recover from. We don't
    redirect back to the MCP client because the failure isn't its fault —
    it's a user/auth issue."""
    safe = (message or "").replace("<", "&lt;").replace(">", "&gt;")
    link = (
        f'<p><a href="{return_uri}">戻る</a></p>' if return_uri else ""
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>Gastrobrain MCP</title>
<style>body{{font-family:system-ui;max-width:520px;margin:80px auto;padding:0 1em;color:#111;line-height:1.6}}
.h{{font-weight:600;font-size:16px;margin-bottom:8px}}.m{{color:#444;font-size:14px}}</style>
</head><body>
<div class="h">Gastrobrain MCP のサインインに失敗しました</div>
<div class="m">{safe}</div>
{link}
</body></html>""",
        status_code=400,
    )


@router.get("/oauth/google-callback")
async def google_callback(request: Request) -> Any:
    s = _settings_or_503()
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    err = request.query_params.get("error")
    if err:
        return _ineligible_html(f"Google からエラーが返されました: {err}")
    if not code or not state:
        return _ineligible_html("code または state が欠落しています。")

    try:
        st = _verify_state_jwt(state)
    except HTTPException as exc:
        return _ineligible_html(exc.detail)

    # Exchange Google's code for an id_token.
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": s.google_oauth_client_id,
                "client_secret": s.google_oauth_client_secret,
                "redirect_uri": f"{_issuer()}/oauth/google-callback",
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code != 200:
        log.warning("google token exchange failed: %s %s", resp.status_code, resp.text[:200])
        return _ineligible_html("Google との認証情報の交換に失敗しました。")
    body = resp.json()
    id_token = body.get("id_token")
    google_access_token = body.get("access_token")
    if not id_token:
        return _ineligible_html("Google が id_token を返しませんでした。")

    # Verify Google's id_token signature via their JWKS. We pass access_token
    # so jose can validate the at_hash claim — otherwise it raises
    # "No access_token provided to compare against at_hash claim". We don't
    # otherwise use Google's access_token (we only need the user's identity).
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            jwks = (await client.get(GOOGLE_JWKS_URI)).json()
        header = jwt.get_unverified_header(id_token)
        kid = header.get("kid")
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key:
            return _ineligible_html("Google の署名鍵が見つかりませんでした。")
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[header.get("alg", "RS256")],
            audience=s.google_oauth_client_id,
            issuer="https://accounts.google.com",
            access_token=google_access_token,
            options={"require_sub": True, "require_exp": True},
        )
    except JWTError as exc:
        log.warning("id_token verification failed: %s", exc)
        return _ineligible_html("Google からのトークンの署名検証に失敗しました。")

    email = (claims.get("email") or "").lower()
    email_verified = claims.get("email_verified") is True
    hd = claims.get("hd")
    if not email_verified:
        return _ineligible_html("Google アカウントのメールが未認証です。")
    if hd != ALLOWED_EMAIL_DOMAIN and not email.endswith(f"@{ALLOWED_EMAIL_DOMAIN}"):
        return _ineligible_html(
            f"このサービスは {ALLOWED_EMAIL_DOMAIN} のアカウントのみ利用できます。"
        )

    import asyncio

    def _lookup_user() -> UUID | None:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT id FROM auth.users WHERE lower(email) = %s LIMIT 1", (email,))
            row = cur.fetchone()
            return row[0] if row else None

    user_id = await asyncio.to_thread(_lookup_user)
    if not user_id:
        return _ineligible_html(
            "Gastrobrain のアカウントが見つかりません。"
            f' まず <a href="https://gastrobrain.app">https://gastrobrain.app</a>'
            " にサインインしてからもう一度お試しください。"
        )

    # Mint our authorization code.
    auth_code = "ac_" + secrets.token_urlsafe(32)
    expires_at_secs = int(time.time()) + AUTH_CODE_TTL_SECONDS

    def _insert_code() -> None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_authorization_codes
                  (code, client_id, user_id, redirect_uri, code_challenge,
                   scope, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s))
                """,
                (
                    auth_code,
                    st["client_id"],
                    str(user_id),
                    st["redirect_uri"],
                    st["code_challenge"],
                    st["scope"],
                    expires_at_secs,
                ),
            )
            c.commit()

    await asyncio.to_thread(_insert_code)

    params = {"code": auth_code}
    if st.get("original_state"):
        params["state"] = st["original_state"]
    sep = "&" if "?" in st["redirect_uri"] else "?"
    return RedirectResponse(url=f"{st['redirect_uri']}{sep}{urlencode(params)}", status_code=302)


# --------------------------------------------------------------------------------------
# /oauth/token  —  code & refresh grants
# --------------------------------------------------------------------------------------


def _token_error(error: str, description: str = "", status_code: int = 400) -> JSONResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status_code)


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    _settings_or_503()
    # OAuth token endpoint is form-encoded per spec.
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        data = {k: form.get(k) for k in form.keys()}
    else:
        # Be lenient: some MCP clients send JSON. Spec says form, but JSON is
        # harmless to accept.
        try:
            data = await request.json()
        except Exception:
            return _token_error("invalid_request", "expected form or JSON body")

    grant_type = (data.get("grant_type") or "").strip()
    if grant_type == "authorization_code":
        return await _grant_authorization_code(data)
    if grant_type == "refresh_token":
        return await _grant_refresh_token(data)
    return _token_error("unsupported_grant_type")


async def _grant_authorization_code(data: dict[str, Any]) -> JSONResponse:
    import asyncio

    code = data.get("code") or ""
    client_id = data.get("client_id") or ""
    redirect_uri = data.get("redirect_uri") or ""
    code_verifier = data.get("code_verifier") or ""

    if not (code and client_id and redirect_uri and code_verifier):
        return _token_error("invalid_request", "missing code/client_id/redirect_uri/code_verifier")

    try:
        client_uuid = UUID(client_id)
    except ValueError:
        return _token_error("invalid_client", "client_id is not a UUID")

    def _consume() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            # SELECT FOR UPDATE so concurrent token swaps can't double-mint.
            cur.execute(
                """
                SELECT user_id, redirect_uri, code_challenge, scope, expires_at, used_at
                FROM oauth_authorization_codes
                WHERE code = %s AND client_id = %s
                FOR UPDATE
                """,
                (code, str(client_uuid)),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="invalid_grant")
            user_id, stored_uri, stored_challenge, scope, expires_at, used_at = row
            if used_at is not None:
                # Replay — revoke any refresh tokens issued from this code if
                # we ever issued one (we shouldn't have, but be defensive).
                cur.execute(
                    "UPDATE oauth_refresh_tokens SET revoked_at = now() WHERE user_id = %s AND client_id = %s AND revoked_at IS NULL",
                    (str(user_id), str(client_uuid)),
                )
                c.commit()
                raise HTTPException(status_code=400, detail="invalid_grant")
            if expires_at.timestamp() < time.time():
                raise HTTPException(status_code=400, detail="invalid_grant")
            if stored_uri != redirect_uri:
                raise HTTPException(status_code=400, detail="invalid_grant")
            if not _verify_pkce_s256(code_verifier, stored_challenge):
                raise HTTPException(status_code=400, detail="invalid_grant")
            cur.execute(
                "UPDATE oauth_authorization_codes SET used_at = now() WHERE code = %s",
                (code,),
            )
            cur.execute("SELECT email FROM auth.users WHERE id = %s", (str(user_id),))
            row2 = cur.fetchone()
            email = (row2[0] or "").lower() if row2 else ""
            c.commit()
            return {
                "user_id": user_id,
                "email": email,
                "scope": scope or DEFAULT_SCOPE,
            }

    try:
        info = await asyncio.to_thread(_consume)
    except HTTPException as exc:
        return _token_error("invalid_grant", str(exc.detail))

    access_token = _mint_access_token(
        user_id=info["user_id"],
        email=info["email"],
        client_id=client_uuid,
        scope=info["scope"],
    )
    refresh_raw = "rt_" + secrets.token_urlsafe(48)
    refresh_hash = _hash_token(refresh_raw)
    refresh_exp_secs = int(time.time()) + REFRESH_TOKEN_TTL_SECONDS

    def _insert_refresh() -> None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_refresh_tokens
                  (token_hash, client_id, user_id, scope, expires_at)
                VALUES (%s, %s, %s, %s, to_timestamp(%s))
                """,
                (
                    refresh_hash,
                    str(client_uuid),
                    str(info["user_id"]),
                    info["scope"],
                    refresh_exp_secs,
                ),
            )
            c.commit()

    await asyncio.to_thread(_insert_refresh)

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_raw,
            "scope": info["scope"],
        }
    )


async def _grant_refresh_token(data: dict[str, Any]) -> JSONResponse:
    import asyncio

    refresh_raw = data.get("refresh_token") or ""
    client_id = data.get("client_id") or ""
    if not (refresh_raw and client_id):
        return _token_error("invalid_request", "missing refresh_token/client_id")
    try:
        client_uuid = UUID(client_id)
    except ValueError:
        return _token_error("invalid_client", "client_id is not a UUID")

    refresh_hash = _hash_token(refresh_raw)

    def _rotate() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, scope, expires_at, revoked_at
                FROM oauth_refresh_tokens
                WHERE token_hash = %s AND client_id = %s
                FOR UPDATE
                """,
                (refresh_hash, str(client_uuid)),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="invalid_grant")
            user_id, scope, expires_at, revoked_at = row
            if revoked_at is not None:
                # Reuse of a revoked refresh token → compromised client.
                # Revoke the entire chain rooted at this user/client pair.
                cur.execute(
                    """
                    UPDATE oauth_refresh_tokens
                    SET revoked_at = now()
                    WHERE user_id = %s AND client_id = %s AND revoked_at IS NULL
                    """,
                    (str(user_id), str(client_uuid)),
                )
                c.commit()
                raise HTTPException(status_code=400, detail="invalid_grant (reuse detected)")
            if expires_at.timestamp() < time.time():
                raise HTTPException(status_code=400, detail="invalid_grant (expired)")

            # Rotate: revoke old, insert new with parent pointer.
            cur.execute(
                "UPDATE oauth_refresh_tokens SET revoked_at = now(), last_used_at = now() WHERE token_hash = %s",
                (refresh_hash,),
            )

            new_raw = "rt_" + secrets.token_urlsafe(48)
            new_hash = _hash_token(new_raw)
            new_exp_secs = int(time.time()) + REFRESH_TOKEN_TTL_SECONDS
            cur.execute(
                """
                INSERT INTO oauth_refresh_tokens
                  (token_hash, client_id, user_id, parent_token_hash, scope, expires_at)
                VALUES (%s, %s, %s, %s, %s, to_timestamp(%s))
                """,
                (new_hash, str(client_uuid), str(user_id), refresh_hash, scope, new_exp_secs),
            )

            cur.execute("SELECT email FROM auth.users WHERE id = %s", (str(user_id),))
            row2 = cur.fetchone()
            email = (row2[0] or "").lower() if row2 else ""
            c.commit()
            return {
                "user_id": user_id,
                "email": email,
                "scope": scope or DEFAULT_SCOPE,
                "new_raw": new_raw,
            }

    try:
        info = await asyncio.to_thread(_rotate)
    except HTTPException as exc:
        return _token_error("invalid_grant", str(exc.detail))

    access_token = _mint_access_token(
        user_id=info["user_id"],
        email=info["email"],
        client_id=client_uuid,
        scope=info["scope"],
    )
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": info["new_raw"],
            "scope": info["scope"],
        }
    )


# --------------------------------------------------------------------------------------
# Sessions API — for the settings modal's "Active sessions" list. Requires
# Supabase JWT (require_user). Lives here rather than web_api.py because it
# operates on the same oauth_refresh_tokens table this module owns.
# --------------------------------------------------------------------------------------


from fastapi import Depends  # noqa: E402  (kept local — only used below)

from gastrobrain.auth import AuthUser, require_user  # noqa: E402


@router.get("/v1/oauth/sessions")
async def list_sessions(user: AuthUser = Depends(require_user)) -> dict[str, list[dict]]:
    import asyncio

    def _do() -> list[dict]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT r.token_hash, r.created_at, r.last_used_at, c.client_name
                FROM oauth_refresh_tokens r
                JOIN oauth_clients c ON c.client_id = r.client_id
                WHERE r.user_id = %s AND r.revoked_at IS NULL
                ORDER BY r.created_at DESC
                """,
                (str(user.user_id),),
            )
            return [
                {
                    "id": r[0],
                    "created_at": r[1].isoformat(),
                    "last_used_at": r[2].isoformat() if r[2] else None,
                    "client_name": r[3] or "MCP client",
                }
                for r in cur.fetchall()
            ]

    return {"sessions": await asyncio.to_thread(_do)}


@router.delete("/v1/oauth/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: str, user: AuthUser = Depends(require_user)) -> None:
    import asyncio

    def _do() -> None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE oauth_refresh_tokens
                SET revoked_at = now()
                WHERE token_hash = %s AND user_id = %s AND revoked_at IS NULL
                """,
                (session_id, str(user.user_id)),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="session not found")
            c.commit()

    await asyncio.to_thread(_do)


__all__ = ["router", "verify_access_token"]
