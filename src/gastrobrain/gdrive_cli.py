"""Google Drive ingestion CLI — meeting transcripts from the company-wide folder.

Two entry points:
  gb-drive-dryrun  — list + dedup-estimate + cost projection. No downloads of
                     bodies, no DB writes, no embedding. Safe to run repeatedly.
  gb-drive-ingest  — download .txt transcripts, dedup by content hash,
                     transcript-aware chunk, embed, write source='gdrive'.

Auth is ADC (see gdrive.py). Run once before either command:
  gcloud auth application-default login \\
    --scopes=openid,https://www.googleapis.com/auth/drive.readonly
"""

from __future__ import annotations

import hashlib

import psycopg
import typer
from rich.console import Console
from rich.table import Table

from gastrobrain.chunker import chunk_transcript
from gastrobrain.config import settings
from gastrobrain.db import conn
from gastrobrain.embed import embed_texts
from gastrobrain.gdrive import DriveClient, DriveFile
from gastrobrain.notepm_cli import (
    _DB_RETRY,
    COHERE_USD_PER_1M,
    _estimate_tokens,
    _TokenRateLimiter,
)

console = Console()

GDRIVE_SOURCE = "gdrive"
GDRIVE_FOLDER_PATH = ["会議録"]


def _clean_title(name: str) -> str:
    return name[:-4] if name.lower().endswith(".txt") else name


def dryrun(
    folder: str = typer.Option(settings.gdrive_folder_id, help="Drive folder id"),
    sample: int = typer.Option(10, help="How many sample files to print"),
) -> None:
    """Count text files in the folder, estimate uniques (dedup by name+size)
    and embed cost. Metadata only — no file bodies are downloaded."""
    console.print(f"[bold]Folder:[/bold] {folder}")
    client = DriveClient()
    files = list(client.list_text_files(folder))
    if not files:
        console.print("[yellow]No text/plain files found (or no Drive access).[/yellow]")
        raise typer.Exit(0)

    # Exact duplicates in this folder share an identical byte size + name.
    # That's a metadata-only proxy for the content-hash dedup done at ingest.
    unique: dict[tuple[str, int], DriveFile] = {}
    for f in files:
        unique.setdefault((f.name, f.size), f)
    dupes = len(files) - len(unique)
    uniq_bytes = sum(f.size for f in unique.values())
    # Japanese UTF-8 ≈ 3 bytes/char; _estimate_tokens uses ~1 token / 1.5 chars.
    est_tokens = int(uniq_bytes / 3 / 1.5)
    est_usd = est_tokens * COHERE_USD_PER_1M / 1_000_000

    console.print(
        f"\n[bold]Files:[/bold] {len(files)}  "
        f"[bold]Unique (by name+size):[/bold] {len(unique)}  "
        f"[bold]Duplicates:[/bold] {dupes}"
    )
    console.print(
        f"[bold]Unique bytes:[/bold] {uniq_bytes:,}  "
        f"[bold]Est. embed tokens:[/bold] ~{est_tokens:,} (~${est_usd:.2f})"
    )
    console.print("[dim]Token/cost are rough (metadata only); the ingest run reports actuals.[/dim]")

    t = Table("modified", "size", "title", title=f"First {min(sample, len(files))} files (newest-first)")
    for f in files[:sample]:
        t.add_row(f.modified_time[:10], f"{f.size:,}", _clean_title(f.name)[:60])
    console.print(t)


def ingest(
    folder: str = typer.Option(settings.gdrive_folder_id, help="Drive folder id"),
    token_budget: int = typer.Option(
        25_000_000,
        help="Hard stop if running token total would exceed this. 25M ≈ $2.50 at $0.10/1M.",
    ),
    limit: int = typer.Option(0, help="Stop after ingesting this many files (0 = no cap). Use a small number to validate first."),
    batch_size: int = typer.Option(96, help="Cohere embed batch size (max 96)"),
) -> None:
    """Backfill Drive meeting transcripts into Supabase as source='gdrive'.

    Calls Cohere embed and costs money. --token-budget hard-stops locally
    before the next file's embeds; the Cohere dashboard limit is the vendor
    fail-safe. Idempotent: a transcript already present (by content hash) is
    skipped, so re-runs are safe and only ingest new/changed files."""
    budget_usd = token_budget * COHERE_USD_PER_1M / 1_000_000
    console.print(f"[bold]Folder:[/bold] {folder}   [bold]Budget:[/bold] {token_budget:,} tokens (~${budget_usd:.2f})")
    if limit:
        console.print(f"[bold]File limit:[/bold] {limit}")

    client = DriveClient()
    files = list(client.list_text_files(folder))
    console.print(f"[bold]Listed {len(files)} text files.[/bold]")

    rate_limiter = _TokenRateLimiter()
    seen_hashes: set[str] = set()
    running_tokens = 0
    ingested = 0
    chunks_total = 0
    skipped_dupe = 0
    skipped_unchanged = 0
    skipped_empty = 0
    scanned = 0
    stop_reason = "exhausted"

    @_DB_RETRY
    def _hash_exists(content_hash: str) -> bool:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM documents WHERE source = %s AND content_hash = %s LIMIT 1",
                (GDRIVE_SOURCE, content_hash),
            )
            return cur.fetchone() is not None

    @_DB_RETRY
    def _persist_with_retry(f, body, chs, titled, embeddings, content_hash):
        with conn() as c, c.cursor() as cur:
            n = _persist(cur, f, body, chs, titled, embeddings, content_hash)
            c.commit()
            return n

    for f in files:
        scanned += 1
        body = client.download_text(f.id)
        if not body.strip():
            skipped_empty += 1
            continue

        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            skipped_dupe += 1
            continue
        if _hash_exists(content_hash):
            skipped_unchanged += 1
            seen_hashes.add(content_hash)
            continue
        seen_hashes.add(content_hash)

        chs = chunk_transcript(body)
        if not chs:
            skipped_empty += 1
            continue

        title = _clean_title(f.name)
        titled = [f"タイトル: {title}\n\n{ch.content}" for ch in chs]
        est = sum(_estimate_tokens(t) for t in titled)
        if running_tokens + est > token_budget:
            stop_reason = f"token budget would be exceeded ({running_tokens + est:,} > {token_budget:,})"
            break

        embeddings: list[list[float]] = []
        for i in range(0, len(titled), batch_size):
            batch = titled[i:i + batch_size]
            slept = rate_limiter.reserve(sum(_estimate_tokens(t) for t in batch))
            if slept > 0:
                console.print(f"  [dim]rate-limit pause: {slept:.1f}s[/dim]")
            embeddings.extend(embed_texts(batch, input_type="search_document"))
        running_tokens += est

        try:
            n = _persist_with_retry(f, body, chs, titled, embeddings, content_hash)
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            console.print(f"  [red]persist failed for {title[:40]}: {type(e).__name__}[/red]")
            continue
        ingested += 1
        chunks_total += n

        if ingested % 25 == 0:
            spent = running_tokens * COHERE_USD_PER_1M / 1_000_000
            console.print(f"  ...ingested {ingested} files, {chunks_total} chunks, ~{running_tokens:,} tokens (~${spent:.2f})")
        if limit and ingested >= limit:
            stop_reason = f"hit --limit {limit}"
            break

    spent = running_tokens * COHERE_USD_PER_1M / 1_000_000
    console.print(f"\n[bold]Done.[/bold] Stop: {stop_reason}")
    console.print(f"  Scanned:                {scanned}")
    console.print(f"  Ingested:               [green]{ingested}[/green] files, [green]{chunks_total}[/green] chunks")
    console.print(f"  Skipped (dup in folder): {skipped_dupe}")
    console.print(f"  Skipped (already in DB): {skipped_unchanged}")
    console.print(f"  Skipped (empty):         {skipped_empty}")
    console.print(f"  [bold]Estimated cost:[/bold] {running_tokens:,} tokens (~${spent:.4f})")


def _persist(cur, f: DriveFile, body: str, chunks, titled, embeddings, content_hash: str) -> int:
    external_id = f.id
    title = _clean_title(f.name)
    cur.execute(
        "SELECT id FROM documents WHERE source = %s AND external_id = %s",
        (GDRIVE_SOURCE, external_id),
    )
    existing = cur.fetchone()
    if existing:
        doc_id = existing[0]
        cur.execute(
            """UPDATE documents
               SET title=%s, url=%s, author=%s, folder_path=%s,
                   updated_at=%s, raw_markdown=%s, content_hash=%s, deleted_at=NULL
               WHERE id=%s""",
            (title, f.web_view_link, None, GDRIVE_FOLDER_PATH,
             f.modified_time, body, content_hash, doc_id),
        )
        cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
    else:
        cur.execute(
            """INSERT INTO documents
                  (source, external_id, title, folder_path, url, author,
                   updated_at, raw_markdown, content_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (GDRIVE_SOURCE, external_id, title, GDRIVE_FOLDER_PATH, f.web_view_link,
             None, f.modified_time, body, content_hash),
        )
        doc_id = cur.fetchone()[0]
    for ch, t, emb in zip(chunks, titled, embeddings, strict=True):
        cur.execute(
            """INSERT INTO chunks
                  (doc_id, ordinal, kind, heading_path, content, token_count, embedding)
               VALUES (%s, %s, 'page', %s, %s, %s, %s)""",
            (doc_id, ch.ordinal, ch.heading_path, t, max(1, len(t) // 2), emb),
        )
    return len(chunks)


def dryrun_cli() -> None:
    typer.run(dryrun)


def ingest_cli() -> None:
    typer.run(ingest)


if __name__ == "__main__":
    dryrun_cli()
