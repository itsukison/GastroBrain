"""NotePM ingestion CLI — currently exposes only a dry-run.

The dry-run resolves the manager YAML against NotePM's live user list,
then counts how many pages would pass the (cutoff + manager-author) filter.
No DB writes, no embedding calls. Safe to run repeatedly.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import psycopg
import typer
import yaml
from rich.console import Console
from rich.table import Table
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gastrobrain.chunker import chunk_markdown
from gastrobrain.config import settings
from gastrobrain.db import conn
from gastrobrain.embed import embed_texts
from gastrobrain.notepm import NotePMClient, Page, User

_DB_RETRY = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((psycopg.OperationalError, psycopg.InterfaceError)),
    reraise=True,
)

console = Console()

NOTEPM_SOURCE = "notepm"
COHERE_USD_PER_1M = 0.10
# Production-tier embed has no per-min token cap at our volumes; this generous
# cap effectively disables the limiter. Lower it to 100_000 to throttle for
# trial-tier keys.
COHERE_TRIAL_TOKENS_PER_MIN = 10_000_000


class _TokenRateLimiter:
    """Sliding 60-second window over Cohere embed token usage.

    Cohere trial keys throttle at 100k tokens/min on embed; production has
    the same per-minute cap but a much higher monthly cap. Before each
    batch, this limiter sleeps until adding `batch_tokens` would keep the
    last 60s under `cap`. Headroom factor < 1.0 keeps us off the cliff
    edge — Cohere counts actual tokens, we estimate, so leave margin."""

    def __init__(self, tokens_per_min: int = COHERE_TRIAL_TOKENS_PER_MIN, headroom: float = 0.85):
        self._cap = int(tokens_per_min * headroom)
        self._events: list[tuple[float, int]] = []

    def reserve(self, batch_tokens: int) -> float:
        """Sleep if needed, then record the batch. Returns seconds slept."""
        slept = 0.0
        while True:
            now = time.monotonic()
            self._events = [(t, n) for t, n in self._events if now - t < 60.0]
            window = sum(n for _, n in self._events)
            if window + batch_tokens <= self._cap or not self._events:
                self._events.append((now, batch_tokens))
                return slept
            oldest = min(t for t, _ in self._events)
            wait = 60.0 - (now - oldest) + 0.3
            time.sleep(wait)
            slept += wait


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


def _load_excluded_notes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {e["code"] for e in (data or {}).get("excluded") or [] if e.get("code")}


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
        excluded_notes = _load_excluded_notes(settings.notepm_excluded_notes_file)
        notes_map = client.list_notes()
        excluded_names = sorted(notes_map.get(code, code) for code in excluded_notes)
        console.print(f"\n[bold]Scanning pages[/bold] (filter: updated_at >= {cutoff_date} AND author/editor ∈ {len(manager_codes)} managers AND note ∉ {len(excluded_notes)} excluded)")
        if excluded_names:
            console.print(f"  [dim]Excluded notes: {', '.join(excluded_names)}[/dim]")
        users_by_code = {u.user_code: u.name for u in users}

        scanned = 0
        matched_count = 0
        excluded_count = 0
        examples: list = []
        stop_reason = "exhausted"
        for page in client.list_pages():
            scanned += 1
            page_dt = datetime.fromisoformat(page.updated_at)
            if page_dt.replace(tzinfo=None) < cutoff_dt.replace(tzinfo=None):
                stop_reason = f"reached cutoff at page #{scanned} (updated_at={page.updated_at[:10]})"
                break
            if page.note_code in excluded_notes:
                excluded_count += 1
                continue
            if page.created_by_user_code in manager_codes or page.updated_by_user_code in manager_codes:
                matched_count += 1
                if len(examples) < sample:
                    examples.append(page)
            if scanned % 500 == 0:
                console.print(f"  ...scanned {scanned}, matched {matched_count}, excluded-by-note {excluded_count}")
            if max_scan and scanned >= max_scan:
                stop_reason = f"hit --max-scan {max_scan}"
                break

        console.print(f"\n[bold]Done.[/bold] Stopped: {stop_reason}")
        console.print(f"  Scanned: {scanned}   Matched: [bold]{matched_count}[/bold]   Excluded by note: {excluded_count}")

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


def _estimate_tokens(s: str) -> int:
    """Conservative bound: 1 token per 1.5 chars. Real Cohere multilingual-v3
    typically uses fewer tokens on pure Japanese prose (closer to 1 per 2-3
    chars), but NotePM bodies contain `<img>` tags and other ASCII markup
    that push the ratio toward 1:1.5. Bounding from the high side keeps the
    --token-budget guard honest."""
    return max(1, int(len(s) / 1.5))


def _persist_page(
    cur,
    page: Page,
    body: str,
    chunks: list,
    titled_contents: list[str],
    embeddings: list[list[float]],
    folder_path: list[str],
    author_name: str,
) -> int:
    external_id = page.page_code
    title = page.title or page.page_code
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    cur.execute(
        "SELECT id, content_hash FROM documents WHERE source = %s AND external_id = %s",
        (NOTEPM_SOURCE, external_id),
    )
    existing = cur.fetchone()
    if existing:
        doc_id = existing[0]
        cur.execute(
            """UPDATE documents
               SET title=%s, url=%s, author=%s, folder_path=%s, note_code=%s,
                   updated_at=%s, raw_markdown=%s, content_hash=%s, deleted_at=NULL
               WHERE id=%s""",
            (title, page.url, author_name, folder_path, page.note_code,
             page.updated_at, body, content_hash, doc_id),
        )
        cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
    else:
        cur.execute(
            """INSERT INTO documents
                  (source, external_id, title, folder_path, note_code, url, author,
                   updated_at, raw_markdown, content_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (NOTEPM_SOURCE, external_id, title, folder_path, page.note_code, page.url,
             author_name, page.updated_at, body, content_hash),
        )
        doc_id = cur.fetchone()[0]
    for ch, titled, emb in zip(chunks, titled_contents, embeddings, strict=True):
        cur.execute(
            """INSERT INTO chunks
                  (doc_id, ordinal, kind, heading_path, content, token_count, embedding)
               VALUES (%s, %s, 'page', %s, %s, %s, %s)""",
            (doc_id, ch.ordinal, ch.heading_path, titled,
             max(1, len(titled) // 2), emb),
        )
    return len(chunks)


def ingest(
    token_budget: int = typer.Option(
        25_000_000,
        help="Hard stop if running token total would exceed this. Default 25M ≈ $2.50 at $0.10/1M.",
    ),
    limit: int = typer.Option(0, help="Stop after ingesting this many pages (0 = no cap). Use a small number for first-run validation."),
    cutoff: str = typer.Option("", help="Override cutoff date (YYYY-MM-DD); empty = use settings.notepm_cutoff_date"),
    batch_size: int = typer.Option(96, help="Cohere embed batch size (max 96)"),
    sweep_deletes: bool = typer.Option(True, help="After a complete scan, soft-delete docs that no longer exist (or no longer qualify) in NotePM. Auto-skipped if the scan stopped early (--limit / token budget)."),
) -> None:
    """Sync NotePM pages matching the manager + cutoff filter into Supabase.

    Idempotent: unchanged pages (content_hash match) are skipped, so re-runs
    only embed new/edited pages. With --sweep-deletes (default), a completed
    scan also soft-deletes docs that disappeared from NotePM — making this a
    full add/update/delete sync suitable for a nightly job.

    This calls Cohere embed_multilingual_v3 and costs money. Two safeties:
      1. --token-budget hard-stops locally before the next page's embeds.
      2. The Cohere dashboard Spending Limit is the vendor-side fail-safe.
    """
    cutoff_date = cutoff or settings.notepm_cutoff_date
    cutoff_dt = datetime.fromisoformat(cutoff_date).replace(tzinfo=None)

    entries = _load_managers(settings.notepm_managers_file)
    budget_usd = token_budget * COHERE_USD_PER_1M / 1_000_000
    console.print(f"[bold]Cutoff:[/bold] {cutoff_date}   [bold]Budget:[/bold] {token_budget:,} tokens (~${budget_usd:.2f})")
    if limit:
        console.print(f"[bold]Page limit:[/bold] {limit}")

    with NotePMClient() as client:
        console.print("[bold]Loading NotePM users + notes...[/bold]")
        users = list(client.list_users())
        by_name = {_normalize(u.name): u for u in users}
        by_email = {u.email.lower(): u for u in users if u.email}
        matched: dict[str, User] = {}
        for entry in entries:
            u = _match_entry(entry, by_name, by_email)
            if u:
                matched[u.user_code] = u
        if len(matched) < len(entries):
            console.print(f"[red]Resolved {len(matched)}/{len(entries)} managers — run gb-notepm-dryrun to debug the YAML first.[/red]")
            raise typer.Exit(code=1)
        manager_codes = set(matched.keys())
        users_by_code = {u.user_code: u.name for u in users}
        notes_map = client.list_notes()
        excluded_notes = _load_excluded_notes(settings.notepm_excluded_notes_file)
        console.print(f"  {len(users)} users, {len(notes_map)} notes loaded, {len(excluded_notes)} notes excluded")

        running_tokens = 0
        pages_ingested = 0
        chunks_ingested = 0
        skipped_unchanged = 0
        skipped_non_manager = 0
        skipped_excluded_note = 0
        skipped_empty = 0
        scanned = 0
        stop_reason = "exhausted"
        rate_limiter = _TokenRateLimiter()

        @_DB_RETRY
        def _load_known() -> tuple[dict[str, str], set[str]]:
            """external_id -> content_hash for all NotePM docs, plus the subset
            currently live (deleted_at IS NULL). One query instead of one
            round-trip per scanned page."""
            with conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT external_id, content_hash, deleted_at IS NULL FROM documents WHERE source = %s",
                    (NOTEPM_SOURCE,),
                )
                rows = cur.fetchall()
            return {r[0]: r[1] for r in rows}, {r[0] for r in rows if r[2]}

        @_DB_RETRY
        def _sweep(codes: list[str]) -> tuple[int, int]:
            """Soft-delete live docs not seen this scan; un-delete reappeared ones."""
            with conn() as c, c.cursor() as cur:
                cur.execute(
                    """UPDATE documents SET deleted_at = now()
                       WHERE source = %s AND deleted_at IS NULL AND NOT (external_id = ANY(%s))""",
                    (NOTEPM_SOURCE, codes),
                )
                deleted = cur.rowcount
                cur.execute(
                    """UPDATE documents SET deleted_at = NULL
                       WHERE source = %s AND deleted_at IS NOT NULL AND external_id = ANY(%s)""",
                    (NOTEPM_SOURCE, codes),
                )
                restored = cur.rowcount
                c.commit()
                return deleted, restored

        @_DB_RETRY
        def _persist_with_retry(page, body, chs, titled, embeddings, folder_path, author_name):
            with conn() as c, c.cursor() as cur:
                n = _persist_page(cur, page, body, chs, titled, embeddings, folder_path, author_name)
                c.commit()
                return n

        known_hashes, live_codes = _load_known()
        console.print(f"  {len(known_hashes)} docs already in DB ({len(live_codes)} live)")
        seen_codes: set[str] = set()
        scan_complete = False

        for page in client.list_pages():
            scanned += 1
            page_dt = datetime.fromisoformat(page.updated_at).replace(tzinfo=None)
            if page_dt < cutoff_dt:
                stop_reason = f"reached cutoff at scan #{scanned}"
                scan_complete = True
                break
            if page.note_code in excluded_notes:
                skipped_excluded_note += 1
                continue
            if not (page.created_by_user_code in manager_codes
                    or page.updated_by_user_code in manager_codes):
                skipped_non_manager += 1
                continue

            body = page.body or ""
            if not body.strip():
                skipped_empty += 1
                continue
            seen_codes.add(page.page_code)

            content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if known_hashes.get(page.page_code) == content_hash:
                skipped_unchanged += 1
                continue

            chs = chunk_markdown(body)
            if not chs:
                skipped_empty += 1
                continue

            titled = [f"タイトル: {page.title}\n\n{ch.content}" for ch in chs]
            est = sum(_estimate_tokens(t) for t in titled)
            if running_tokens + est > token_budget:
                stop_reason = f"token budget would be exceeded ({running_tokens + est:,} > {token_budget:,})"
                break

            embeddings: list[list[float]] = []
            for i in range(0, len(titled), batch_size):
                batch = titled[i:i + batch_size]
                batch_est = sum(_estimate_tokens(t) for t in batch)
                slept = rate_limiter.reserve(batch_est)
                if slept > 0:
                    console.print(f"  [dim]rate-limit pause: {slept:.1f}s[/dim]")
                embeddings.extend(embed_texts(batch, input_type="search_document"))
            running_tokens += est

            folder_path = [notes_map[page.note_code]] if notes_map.get(page.note_code) else []
            author_name = users_by_code.get(page.created_by_user_code, page.created_by_user_code)
            try:
                n = _persist_with_retry(page, body, chs, titled, embeddings, folder_path, author_name)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                console.print(f"  [red]persist failed for {page.page_code} ({page.title[:40]}): {type(e).__name__}[/red]")
                continue
            pages_ingested += 1
            chunks_ingested += n

            if pages_ingested % 50 == 0:
                spent = running_tokens * COHERE_USD_PER_1M / 1_000_000
                console.print(f"  ...ingested {pages_ingested} pages, {chunks_ingested} chunks, ~{running_tokens:,} tokens (~${spent:.2f})")
            if limit and pages_ingested >= limit:
                stop_reason = f"hit --limit {limit}"
                break

        if stop_reason == "exhausted":
            scan_complete = True

        deleted = restored = 0
        if sweep_deletes and scan_complete:
            candidates = live_codes - seen_codes
            max_deletes = max(50, len(live_codes) // 20)
            if len(candidates) > max_deletes:
                console.print(f"[red]Sweep aborted: {len(candidates)} docs would be deleted (> {max_deletes} guard). Likely an API or filter regression — investigate before sweeping.[/red]")
            else:
                deleted, restored = _sweep(sorted(seen_codes))
        elif sweep_deletes:
            console.print("[yellow]Sweep skipped: scan did not complete (budget/limit stop).[/yellow]")

        spent = running_tokens * COHERE_USD_PER_1M / 1_000_000
        console.print(f"\n[bold]Done.[/bold] Stop: {stop_reason}")
        console.print(f"  Scanned:               {scanned}")
        console.print(f"  Ingested:              [green]{pages_ingested}[/green] pages, [green]{chunks_ingested}[/green] chunks")
        console.print(f"  Skipped (unchanged):   {skipped_unchanged}")
        console.print(f"  Skipped (non-manager): {skipped_non_manager}")
        console.print(f"  Skipped (excluded note): {skipped_excluded_note}")
        console.print(f"  Skipped (empty body):  {skipped_empty}")
        console.print(f"  Soft-deleted:          {deleted}   Restored: {restored}")
        console.print(f"  [bold]Estimated cost:[/bold] {running_tokens:,} tokens (~${spent:.4f})")


def ingest_cli() -> None:
    typer.run(ingest)


if __name__ == "__main__":
    cli()
