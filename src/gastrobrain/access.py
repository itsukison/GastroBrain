"""Corpus access resolution — the single source of truth shared by every
surface (web, Slack, MCP).

The model is set-based and NotePM-derived (see migrations/011_notepm_acl.sql):
a person sees a NotePM document iff their NotePM `user_code` is in that note's
resolved access set, or the note is public. Non-NotePM documents (slack/gdrive/
manual) are unrestricted. Identity is keyed by email across all surfaces; the
nightly sync stamps `members.notepm_user_code` from the NotePM API, so an email
with no NotePM account resolves to public-only (fail-closed).

Slack callers are additionally resolvable by their cached `members.slack_user_id`
so we don't hit the Slack API on every question.

This module is intentionally DB-only — it imports no surface code, so web_api,
slack_app and mcp_server can all depend on it without cycles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gastrobrain.db import conn

log = logging.getLogger("gastrobrain.access")


@dataclass(frozen=True)
class AccessScope:
    """Per-request corpus visibility.

    - `see_all` (break-glass / operator tokens, local CLIs): every document.
    - otherwise: the caller's NotePM identity. `user_code` gates NotePM docs by
      note membership; non-NotePM docs and public notes are always visible.
      `user_code is None` (no NotePM account matched) → public notes only.
    """

    user_code: str | None = None
    see_all: bool = False


# Default fail-closed scope (no NotePM identity → public notes + non-NotePM only).
PUBLIC_ONLY = AccessScope()
# Break-glass: operator / env-var MCP tokens and local CLIs see everything.
SEE_ALL = AccessScope(see_all=True)


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    return e or None


def resolve_access(email: str | None) -> AccessScope:
    """Access scope for an email. Public-only if the email is missing, has no
    member row, or the member has no NotePM account linked."""
    email = _normalize_email(email)
    if not email:
        return PUBLIC_ONLY
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT notepm_user_code FROM members WHERE email = %s", (email,))
        row = cur.fetchone()
    return AccessScope(user_code=row[0]) if row and row[0] else PUBLIC_ONLY


def is_admin(email: str | None) -> bool:
    """Whether this email may open the org view."""
    email = _normalize_email(email)
    if not email:
        return False
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT is_admin FROM members WHERE email = %s", (email,))
        row = cur.fetchone()
    return bool(row and row[0])


def scope_by_slack_id(slack_user_id: str) -> AccessScope | None:
    """Access scope for an already-linked Slack user. Returns None when no member
    row carries this slack_user_id yet (caller should then resolve the Slack email
    and call `link_slack_id`). A linked member with no NotePM account → public-only."""
    if not slack_user_id:
        return None
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT notepm_user_code FROM members WHERE slack_user_id = %s",
            (slack_user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return AccessScope(user_code=row[0]) if row[0] else PUBLIC_ONLY


def link_slack_id(email: str | None, slack_user_id: str) -> AccessScope:
    """Cache the Slack→email mapping onto the member with this email and return
    their scope. Public-only if no member exists for the email (an unknown Slack
    user stays public-only). The slack_user_id column is UNIQUE, so a re-link of
    an id already pointing elsewhere is cleared first."""
    email = _normalize_email(email)
    if not email or not slack_user_id:
        return PUBLIC_ONLY
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE members SET slack_user_id = NULL WHERE slack_user_id = %s AND email <> %s",
            (slack_user_id, email),
        )
        cur.execute(
            """
            UPDATE members m
            SET slack_user_id = %s, updated_at = now()
            WHERE m.email = %s
            RETURNING m.notepm_user_code
            """,
            (slack_user_id, email),
        )
        row = cur.fetchone()
        c.commit()
    return AccessScope(user_code=row[0]) if row and row[0] else PUBLIC_ONLY


def recompute_document_levels() -> None:
    """Re-stamp documents.min_level from the current folder_acl rules. Legacy of
    the clearance-ladder model (migrations/006_rbac.sql); still called by the
    folder-acl admin endpoints until they are retired."""
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT recompute_document_levels()")
        c.commit()
    log.info("recomputed documents.min_level from folder_acl")
