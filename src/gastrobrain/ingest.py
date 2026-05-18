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

app = typer.Typer(add_completion=False, help="Ingest markdown files from a directory")
console = Console()


@app.command()
def main(
    corpus_dir: Path = typer.Argument(..., help="Directory containing .md files"),
    source: str = typer.Option("manual", help="Source label: 'manual' or 'notepm'"),
    batch_size: int = typer.Option(96, help="Embedding batch size (Cohere limit: 96)"),
) -> None:
    if not corpus_dir.is_dir():
        console.print(f"[red]Not a directory:[/red] {corpus_dir}")
        raise typer.Exit(1)

    files = sorted(corpus_dir.glob("*.md"))
    files = [f for f in files if f.name != "README.md"]
    if not files:
        console.print(f"[yellow]No .md files in[/yellow] {corpus_dir}")
        raise typer.Exit(0)

    console.print(f"Found [bold]{len(files)}[/bold] markdown files")

    docs_ingested = 0
    docs_skipped = 0
    chunks_total = 0

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as prog:
        for path in files:
            task = prog.add_task(f"[cyan]{path.name}[/cyan]", total=None)
            result = _ingest_one(path, source, batch_size)
            prog.remove_task(task)
            if result == "skipped":
                docs_skipped += 1
                console.print(f"  [dim]skip[/dim] {path.name} (unchanged)")
            else:
                docs_ingested += 1
                chunks_total += result
                console.print(f"  [green]ok[/green]   {path.name} ({result} chunks)")

    console.print(
        f"\n[bold]Done.[/bold] {docs_ingested} ingested, "
        f"{docs_skipped} skipped, {chunks_total} chunks total."
    )


def _ingest_one(path: Path, source: str, batch_size: int) -> int | str:
    raw = path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    body = post.content
    meta = post.metadata

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


def _with_title_prefix(title: str, chunk_content: str) -> str:
    """Prepend `タイトル: <title>` to the chunk content so both lexical and
    dense indexes can find chunks by their parent document's title."""
    return f"タイトル: {title}\n\n{chunk_content}"


if __name__ == "__main__":
    app()
