"""Slack-derived access sync (migrations/012_slack_acl.sql).

Mirrors notepm_acl.py for Slack: rebuilds the set-based access tables from the
live Slack API so a person sees a Slack document iff its channel is public, or
they are a member of that private channel. Slack is the source of truth — there
is no manual permission step.

Effective access for a private channel = its current member list
(conversations.members). Public channels are flagged is_private=false and visible
to everyone; they get no access rows. Membership is expanded into flat
(channel_id, slack_user_id) rows so the runtime gate is a single indexed lookup.

The rebuild runs in one transaction (full refresh, idempotent): channels the bot
can no longer see are dropped (cascading their access rows → those docs become
inaccessible, fail-closed), every member's slack_user_id is re-derived by email,
and a member row is upserted for each Slack user with an email so Slack-only
users (and web/MCP callers) resolve to their channel access with no admin action.

Designed to run nightly after gb-slack-ingest. Requires the users:read.email
scope for the email→slack_user_id links; without it, links come back empty and
everyone falls back to public-channel-only access for Slack.
"""

from __future__ import annotations

import typer
from rich.console import Console

from gastrobrain.db import conn
from gastrobrain.slack_ingest import SlackClient

console = Console()


def _resolve(client: SlackClient) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Pull channels, private-channel members, and user emails from the API.
    Returns (channel_rows, access_rows, user_links)."""
    channel_rows: list[tuple] = []   # (channel_id, name, is_private)
    access_rows: list[tuple] = []    # (channel_id, slack_user_id)
    for ch in client.list_channels():
        channel_rows.append((ch["id"], ch["name"], ch["is_private"]))
        if ch["is_private"]:
            access_rows.extend((ch["id"], uid) for uid in client.channel_members(ch["id"]))

    user_links: list[tuple] = []     # (email, slack_user_id)
    for email, uid in client.users_with_email():
        e = email.strip().lower()
        if e:
            user_links.append((e, uid))

    return channel_rows, access_rows, user_links


def sync_acl(dry_run: bool = False) -> dict[str, int]:
    client = SlackClient()
    channel_rows, access_rows, user_links = _resolve(client)

    stats = {
        "channels": len(channel_rows),
        "private_channels": sum(1 for r in channel_rows if r[2]),
        "access_rows": len(access_rows),
        "linked_users": len(user_links),
    }
    if dry_run:
        return stats

    channel_ids = [r[0] for r in channel_rows]
    with conn() as c, c.cursor() as cur:
        cur.executemany(
            """INSERT INTO slack_channels (channel_id, name, is_private, synced_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (channel_id) DO UPDATE
                 SET name = EXCLUDED.name, is_private = EXCLUDED.is_private, synced_at = now()""",
            channel_rows,
        )
        # Drop channels the bot can no longer see — cascades their access rows.
        cur.execute("DELETE FROM slack_channels WHERE NOT (channel_id = ANY(%s))", (channel_ids,))
        # Rebuild the access set from scratch (channels upserted first → FK ok).
        cur.execute("DELETE FROM slack_channel_access")
        cur.executemany(
            "INSERT INTO slack_channel_access (channel_id, slack_user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            access_rows,
        )
        # Re-derive every member's Slack link by email; upsert a row per Slack
        # user (preserving notepm_user_code / is_admin on existing rows). Clear
        # first so the UNIQUE slack_user_id never collides on a re-link.
        cur.execute("UPDATE members SET slack_user_id = NULL")
        cur.executemany(
            """INSERT INTO members (email, slack_user_id, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (email) DO UPDATE
                 SET slack_user_id = EXCLUDED.slack_user_id, updated_at = now()""",
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
        f"[bold]{label}[/bold]: {stats['channels']} channels "
        f"({stats['private_channels']} private), {stats['access_rows']} membership rows, "
        f"{stats['linked_users']} Slack users linked by email."
    )
    if stats["linked_users"] == 0:
        console.print(
            "[yellow]No email links — the users:read.email scope is likely missing. "
            "Private-channel gating will fall back to public-only until it's added.[/yellow]"
        )


def sync_cli() -> None:
    typer.run(sync)


if __name__ == "__main__":
    sync_cli()
