"""NotePM-derived access sync (PRD §4; migrations/011_notepm_acl.sql).

Rebuilds the set-based access tables from the live NotePM API so that a person
sees a NotePM note iff their NotePM user_code is in that note's resolved access
set, or the note is public. NotePM is the source of truth — there is no manual
folder-permission step. Designed to run nightly alongside `gb-notepm-ingest`.

Effective access for a note = explicit `users` ∪ (members of each group on the
note). Public notes (scope='open') are flagged and visible to everyone. Group
membership is expanded into flat (note_code, user_code) rows here so the runtime
retrieval gate is a single indexed membership lookup.

The whole rebuild runs in one transaction (full refresh, idempotent): notes that
vanished from NotePM are dropped (cascading their access rows), every member's
`notepm_user_code` is re-derived by email match, and a member row is upserted for
every NotePM user so Slack/web/MCP sign-ins resolve to their access with no admin
action.
"""

from __future__ import annotations

import typer
from rich.console import Console

from gastrobrain.db import conn
from gastrobrain.notepm import NotePMClient

console = Console()


def _resolve(client: NotePMClient) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Pull everything from the API and compute the rows to write.
    Returns (note_rows, access_rows, user_links)."""
    group_cache: dict[str, list[str]] = {}

    def members_of(group: str) -> list[str]:
        if group not in group_cache:
            group_cache[group] = client.group_members(group)
        return group_cache[group]

    note_rows: list[tuple] = []      # (note_code, name, is_public)
    access_rows: list[tuple] = []    # (note_code, user_code)
    for n in client.list_notes_acl():
        allowed: set[str] = set(n["users"])
        for g in n["groups"]:
            allowed.update(members_of(g))
        note_rows.append((n["note_code"], n["name"], n["scope"] == "open"))
        access_rows.extend((n["note_code"], uc) for uc in allowed)

    user_links: list[tuple] = []     # (email, user_code)
    for u in client.list_users():
        email = u.email.strip().lower()
        if email:
            user_links.append((email, u.user_code))

    return note_rows, access_rows, user_links


def sync_acl(dry_run: bool = False) -> dict[str, int]:
    with NotePMClient() as client:
        note_rows, access_rows, user_links = _resolve(client)

    stats = {
        "notes": len(note_rows),
        "public_notes": sum(1 for r in note_rows if r[2]),
        "access_rows": len(access_rows),
        "linked_users": len(user_links),
    }
    if dry_run:
        return stats

    note_codes = [r[0] for r in note_rows]
    with conn() as c, c.cursor() as cur:
        cur.executemany(
            """INSERT INTO notepm_notes (note_code, name, is_public, synced_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (note_code) DO UPDATE
                 SET name = EXCLUDED.name, is_public = EXCLUDED.is_public, synced_at = now()""",
            note_rows,
        )
        # Drop notes that disappeared from NotePM — cascades their access rows.
        cur.execute("DELETE FROM notepm_notes WHERE NOT (note_code = ANY(%s))", (note_codes,))
        # Rebuild the access set from scratch (notes are upserted first → FK ok).
        cur.execute("DELETE FROM notepm_note_access")
        cur.executemany(
            "INSERT INTO notepm_note_access (note_code, user_code) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            access_rows,
        )
        # Re-derive every member's NotePM link; upsert a row for each NotePM user
        # (preserving role_id / is_admin / slack_user_id on existing rows).
        cur.execute("UPDATE members SET notepm_user_code = NULL")
        cur.executemany(
            """INSERT INTO members (email, notepm_user_code, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (email) DO UPDATE
                 SET notepm_user_code = EXCLUDED.notepm_user_code, updated_at = now()""",
            user_links,
        )
        c.commit()
    return stats


def sync(
    dry_run: bool = typer.Option(False, help="Compute and print counts without writing to the DB."),
) -> None:
    stats = sync_acl(dry_run=dry_run)
    label = "DRY RUN — would sync" if dry_run else "synced"
    console.print(
        f"[bold]{label}[/bold]: {stats['notes']} notes "
        f"({stats['public_notes']} public), {stats['access_rows']} access rows, "
        f"{stats['linked_users']} NotePM users linked by email."
    )


def sync_cli() -> None:
    typer.run(sync)


if __name__ == "__main__":
    sync_cli()
