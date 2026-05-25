"""NotePM ingestion CLI — currently exposes only a dry-run.

The dry-run resolves the manager YAML against NotePM's live user list,
then counts how many pages would pass the (cutoff + manager-author) filter.
No DB writes, no embedding calls. Safe to run repeatedly.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from gastrobrain.config import settings
from gastrobrain.notepm import NotePMClient, User

console = Console()


def _normalize(s: str) -> str:
    """NFKC + strip all whitespace + lowercase. Lets '草壁　匠' (full-width space)
    match '草壁 匠', and English names match case-insensitively."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


def _load_managers(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    entries = (data or {}).get("managers") or []
    if not entries:
        raise typer.BadParameter(f"No managers found in {path}")
    return entries


def _match_entry(entry: dict, users_by_name: dict[str, User], users_by_email: dict[str, User]) -> User | None:
    if (ja := entry.get("name_ja")):
        if u := users_by_name.get(_normalize(ja)):
            return u
    if (en := entry.get("name_en")):
        if u := users_by_name.get(_normalize(en)):
            return u
    if (email := entry.get("email")):
        if u := users_by_email.get(email.lower()):
            return u
    return None


def main(
    sample: int = typer.Option(10, help="How many sample matched pages to print"),
    cutoff: str = typer.Option("", help="Override cutoff date (YYYY-MM-DD). Empty = use settings.notepm_cutoff_date"),
    max_scan: int = typer.Option(0, help="Stop after scanning this many pages (0 = no cap). Useful for quick checks."),
) -> None:
    cutoff_date = cutoff or settings.notepm_cutoff_date
    cutoff_dt = datetime.fromisoformat(cutoff_date)
    managers_file = settings.notepm_managers_file

    console.print(f"[bold]Cutoff:[/bold] {cutoff_date}   [bold]Managers file:[/bold] {managers_file}")
    entries = _load_managers(managers_file)

    with NotePMClient() as client:
        console.print(f"\n[bold]Loading NotePM users...[/bold]")
        users = list(client.list_users())
        console.print(f"  {len(users)} users")

        by_name = {_normalize(u.name): u for u in users}
        by_email = {u.email.lower(): u for u in users if u.email}
        matched_users: dict[str, User] = {}
        t = Table("YAML entry", "→ NotePM name", "user_code", "email", title="Manager resolution")
        unmatched = 0
        for entry in entries:
            label = entry.get("name_ja") or entry.get("name_en") or entry.get("email") or "?"
            u = _match_entry(entry, by_name, by_email)
            if u:
                matched_users[u.user_code] = u
                t.add_row(label, u.name, u.user_code, u.email or "-")
            else:
                unmatched += 1
                t.add_row(f"[red]{label}[/red]", "[red]NO MATCH[/red]", "-", "-")
        console.print(t)
        console.print(f"\n[bold]Resolved {len(matched_users)}/{len(entries)} managers.[/bold]")
        if unmatched:
            console.print(f"[yellow]Fix YAML or confirm with Itsuki, then re-run.[/yellow]")
            console.print("[dim]Tip: dump users with --max-scan 0 to scan the full user list above.[/dim]")
            raise typer.Exit(code=1)

        manager_codes = set(matched_users.keys())
        console.print(f"\n[bold]Scanning pages[/bold] (filter: updated_at >= {cutoff_date} AND author/editor ∈ {len(manager_codes)} managers)")
        users_by_code = {u.user_code: u.name for u in users}

        scanned = 0
        matched_count = 0
        examples: list = []
        stop_reason = "exhausted"
        for page in client.list_pages():
            scanned += 1
            page_dt = datetime.fromisoformat(page.updated_at)
            if page_dt.replace(tzinfo=None) < cutoff_dt.replace(tzinfo=None):
                stop_reason = f"reached cutoff at page #{scanned} (updated_at={page.updated_at[:10]})"
                break
            if page.created_by_user_code in manager_codes or page.updated_by_user_code in manager_codes:
                matched_count += 1
                if len(examples) < sample:
                    examples.append(page)
            if scanned % 500 == 0:
                console.print(f"  ...scanned {scanned}, matched {matched_count}")
            if max_scan and scanned >= max_scan:
                stop_reason = f"hit --max-scan {max_scan}"
                break

        console.print(f"\n[bold]Done.[/bold] Stopped: {stop_reason}")
        console.print(f"  Scanned: {scanned}   Matched: [bold]{matched_count}[/bold]")

        if examples:
            t2 = Table("page_code", "updated_at", "title", "creator", "editor", title=f"First {len(examples)} matches")
            for p in examples:
                t2.add_row(
                    p.page_code,
                    p.updated_at[:10],
                    (p.title[:48] + "…") if len(p.title) > 48 else p.title,
                    users_by_code.get(p.created_by_user_code, p.created_by_user_code or "-"),
                    users_by_code.get(p.updated_by_user_code, p.updated_by_user_code or "-"),
                )
            console.print(t2)


def cli() -> None:
    typer.run(main)


if __name__ == "__main__":
    cli()
