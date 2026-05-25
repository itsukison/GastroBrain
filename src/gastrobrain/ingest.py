import hashlib
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import typer
from pgvector.psycopg import register_vector
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from gastrobrain.chunker import chunk_markdown
from gastrobrain.db import conn
from gastrobrain.embed import embed_texts

app = typer.Typer(add_completion=False, help="Ingest markdown and PDF files from a directory")
console = Console()

SUPPORTED_SUFFIXES = {".md", ".pdf"}


@app.command()
def main(
    corpus_dir: Path = typer.Argument(..., help="Directory containing .md and/or .pdf files"),
    source: str = typer.Option("manual", help="Source label: 'manual' or 'notepm'"),
    batch_size: int = typer.Option(96, help="Embedding batch size (Cohere limit: 96)"),
    archive: bool = typer.Option(
        True,
        "--archive/--no-archive",
        help="Move successfully processed files to <corpus_dir>/_ingested/ (default on)",
    ),
) -> None:
    if not corpus_dir.is_dir():
        console.print(f"[red]Not a directory:[/red] {corpus_dir}")
        raise typer.Exit(1)

    archive_dir = corpus_dir / "_ingested"
    files = sorted(
        f for f in corpus_dir.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES
    )
    files = [f for f in files if f.name != "README.md"]
    if not files:
        console.print(f"[yellow]No .md or .pdf files in[/yellow] {corpus_dir}")
        raise typer.Exit(0)

    console.print(f"Found [bold]{len(files)}[/bold] files (.md / .pdf)")

    docs_ingested = 0
    docs_skipped = 0
    docs_empty = 0
    chunks_total = 0

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as prog:
        for path in files:
            task = prog.add_task(f"[cyan]{path.name}[/cyan]", total=None)
            result = _ingest_one(path, source, batch_size)
            prog.remove_task(task)
            if result == "skipped":
                docs_skipped += 1
                console.print(f"  [dim]skip[/dim] {path.name} (unchanged)")
                if archive:
                    _archive_file(path, archive_dir)
            elif isinstance(result, int) and result == 0:
                docs_empty += 1
                console.print(
                    f"  [yellow]empty[/yellow] {path.name} (no extractable text — left in place)"
                )
            else:
                docs_ingested += 1
                chunks_total += result
                console.print(f"  [green]ok[/green]   {path.name} ({result} chunks)")
                if archive:
                    _archive_file(path, archive_dir)

    summary = (
        f"\n[bold]Done.[/bold] {docs_ingested} ingested, "
        f"{docs_skipped} skipped, {chunks_total} chunks total."
    )
    if docs_empty:
        summary += f" [yellow]{docs_empty} empty[/yellow] (likely scanned PDFs — OCR needed)."
    if archive:
        summary += f"\nArchived to [dim]{archive_dir}[/dim]."
    console.print(summary)


def _archive_file(path: Path, archive_dir: Path) -> None:
    """Move a processed file into the archive dir. If a same-named file already
    exists there (e.g., re-ingested after edit), append a timestamp suffix."""
    archive_dir.mkdir(exist_ok=True)
    dest = archive_dir / path.name
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = archive_dir / f"{path.stem}.{stamp}{path.suffix}"
    path.rename(dest)


def _ingest_one(path: Path, source: str, batch_size: int) -> int | str:
    body, meta = _load_document(path)
    if not body.strip():
        return 0

    external_id = str(meta.get("external_id") or path.stem)
    # Prefer filename for manual corpus — H1s in our docs tend to be section
    # labels ("ゴール", "出店手続き") rather than document titles.
    # Frontmatter title overrides; for NotePM, the API will provide the page title.
    title = str(meta.get("title") or path.stem)
    url = meta.get("url")
    author = meta.get("author")
    folder_path = meta.get("folder_path") or []
    if isinstance(folder_path, str):
        folder_path = [folder_path]

    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    updated_at = datetime.now(timezone.utc)

    with conn() as c, c.cursor() as cur:
        register_vector(c)
        cur.execute(
            "SELECT id, content_hash FROM documents WHERE source = %s AND external_id = %s",
            (source, external_id),
        )
        existing = cur.fetchone()
        if existing and existing[1] == content_hash:
            return "skipped"

        chunks = chunk_markdown(body)
        if not chunks:
            return 0

        # Inject the document title into each chunk's content so both lexical
        # (PGroonga) and dense (embedding) search can find chunks via the
        # title. Without this, files like "TTS_デイリーチェックリスト" are
        # only findable by content terms, which often don't match the filename.
        titled = [_with_title_prefix(title, ch.content) for ch in chunks]

        embeddings = []
        for i in range(0, len(titled), batch_size):
            batch = titled[i : i + batch_size]
            embeddings.extend(embed_texts(batch, input_type="search_document"))

        if existing:
            doc_id = existing[0]
            cur.execute(
                """
                UPDATE documents
                SET title=%s, url=%s, author=%s, folder_path=%s,
                    updated_at=%s, raw_markdown=%s, content_hash=%s, deleted_at=NULL
                WHERE id=%s
                """,
                (title, url, author, folder_path, updated_at, body, content_hash, doc_id),
            )
            cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
        else:
            cur.execute(
                """
                INSERT INTO documents
                  (source, external_id, title, folder_path, url, author,
                   updated_at, raw_markdown, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (source, external_id, title, folder_path, url, author,
                 updated_at, body, content_hash),
            )
            doc_id = cur.fetchone()[0]

        for ch, content, emb in zip(chunks, titled, embeddings, strict=True):
            cur.execute(
                """
                INSERT INTO chunks
                  (doc_id, ordinal, kind, heading_path, content, token_count, embedding)
                VALUES (%s, %s, 'page', %s, %s, %s, %s)
                """,
                (doc_id, ch.ordinal, ch.heading_path, content,
                 max(1, len(content) // 2), emb),
            )
        c.commit()
        return len(chunks)


def _load_document(path: Path) -> tuple[str, dict]:
    """Load a corpus file as (markdown_body, metadata).

    Markdown files are parsed with frontmatter. PDFs are converted to markdown
    via pymupdf4llm and carry no frontmatter — title/url/etc. fall back to
    filename-derived defaults in the caller.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pymupdf4llm
        body = pymupdf4llm.to_markdown(str(path), show_progress=False)
        return body, {}
    raw = path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    return post.content, dict(post.metadata)


def _with_title_prefix(title: str, chunk_content: str) -> str:
    """Prepend `タイトル: <title>` to the chunk content so both lexical and
    dense indexes can find chunks by their parent document's title."""
    return f"タイトル: {title}\n\n{chunk_content}"


if __name__ == "__main__":
    app()
