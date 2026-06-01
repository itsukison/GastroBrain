"""Clearance-level resolution — the single source of truth shared by every
surface (web, Slack, MCP).

The model is a hierarchical ladder (see migrations/006_rbac.sql): a person holds
at most one role, a role has a numeric `level`, and a document is visible when
the caller's level ≥ the document's `min_level`. Unmapped identities resolve to
level 0 (they see only unrestricted documents — fail-closed on anything gated).

Identity is keyed by email across all surfaces. Slack callers are additionally
resolvable by their cached `members.slack_user_id` so we don't hit the Slack API
on every question.

This module is intentionally DB-only — it imports no surface code, so web_api,
slack_app and mcp_server can all depend on it without cycles.
"""

from __future__ import annotations

import logging

from gastrobrain.db import conn

log = logging.getLogger("gastrobrain.access")

# Break-glass / operator access (env-var MCP tokens). Far above any folder rule
# (which is capped at the top role level, 4), so it sees everything regardless
# of how restrictive a folder rule is set.
BREAK_GLASS_LEVEL = 1_000_000


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    return e or None


def resolve_level(email: str | None) -> int:
    """Clearance level for an email. 0 if the email is missing, has no member
    row, or the member has no role assigned (all → unrestricted-only)."""
    email = _normalize_email(email)
    if not email:
        return 0
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT r.level
            FROM members m
            LEFT JOIN roles r ON r.id = m.role_id
            WHERE m.email = %s
            """,
            (email,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def is_admin(email: str | None) -> bool:
    """Whether this email may open the org view / manage roles + folder rules."""
    email = _normalize_email(email)
    if not email:
        return False
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT is_admin FROM members WHERE email = %s", (email,))
        row = cur.fetchone()
    return bool(row and row[0])


def level_by_slack_id(slack_user_id: str) -> int | None:
    """Clearance level for an already-linked Slack user. Returns None when no
    member row carries this slack_user_id yet (caller should then resolve the
    Slack email and call `link_slack_id`). A linked member with no role → 0."""
    if not slack_user_id:
        return None
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT r.level
            FROM members m
            LEFT JOIN roles r ON r.id = m.role_id
            WHERE m.slack_user_id = %s
            """,
            (slack_user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return int(row[0]) if row[0] is not None else 0


def link_slack_id(email: str | None, slack_user_id: str) -> int:
    """Cache the Slack→email mapping onto the member with this email and return
    their level. No-op returning 0 if no member exists for the email (an unknown
    Slack user stays unrestricted-only). The slack_user_id column is UNIQUE, so
    a re-link of an id already pointing elsewhere is cleared first."""
    email = _normalize_email(email)
    if not email or not slack_user_id:
        return 0
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
            RETURNING (SELECT r.level FROM roles r WHERE r.id = m.role_id)
            """,
            (slack_user_id, email),
        )
        row = cur.fetchone()
        c.commit()
    return int(row[0]) if row and row[0] is not None else 0


def recompute_document_levels() -> None:
    """Re-stamp documents.min_level from the current folder_acl rules. Call
    after any folder_acl insert/update/delete."""
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT recompute_document_levels()")
        c.commit()
    log.info("recomputed documents.min_level from folder_acl")
