import time
from uuid import UUID

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule

from gastrobrain.access import SEE_ALL
from gastrobrain.db import conn
from gastrobrain.generate import answer
from gastrobrain.retrieve import RetrievedChunk, retrieve

app = typer.Typer(add_completion=False, help="Interactive Q&A with feedback capture")
console = Console()

HELP = """\
Commands:
  :h  / :help     this message
  :q  / :quit     exit
  :s  <question>  ask with full chunk dump (verbose)
Anything else is treated as a question.

After each answer, respond with one of:
  y / 👍         positive feedback
  n / 👎         negative feedback
  <Enter>        skip
  <text>         negative feedback with note (anything else)
"""


@app.command()
def main(
    user: str = typer.Option("test", help="User ID stored with each query"),
) -> None:
    console.print("[bold cyan]Gastrobrain[/bold cyan] — test mode. Type [bold]:h[/bold] for help, [bold]:q[/bold] to quit.\n")

    while True:
        try:
            line = console.input("[bold magenta]❯[/bold magenta] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]exit[/dim]")
            break

        if not line:
            continue
        if line in (":q", ":quit"):
            break
        if line in (":h", ":help"):
            console.print(HELP)
            continue

        verbose = False
        if line.startswith(":s "):
            verbose = True
            line = line[3:].strip()

        _ask_once(line, user=user, verbose=verbose)


def _ask_once(question: str, *, user: str, verbose: bool) -> None:
    t0 = time.perf_counter()
    chunks = retrieve(question, scope=SEE_ALL)
    t_retrieve = time.perf_counter() - t0

    _print_chunk_summary(chunks, verbose=verbose)

    t1 = time.perf_counter()
    result = answer(question, chunks)
    t_generate = time.perf_counter() - t1
    t_total = time.perf_counter() - t0

    console.print(Rule(style="dim"))
    console.print(Markdown(result.answer))
    console.print(Rule(style="dim"))
    console.print(
        f"[dim]retrieve={t_retrieve*1000:.0f}ms  generate={t_generate*1000:.0f}ms  "
        f"total={t_total*1000:.0f}ms  in={result.input_tokens}  out={result.output_tokens}  "
        f"cache_read={result.cache_read_input_tokens}[/dim]"
    )

    qid = _insert_query(
        user_id=user,
        question=question,
        result_answer=result.answer,
        cited=[c.chunk_id for c in chunks],
        latency_ms=int(t_total * 1000),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )

    _capture_feedback(qid)
    console.print()


def _print_chunk_summary(chunks: list[RetrievedChunk], *, verbose: bool) -> None:
    if not chunks:
        console.print("[red]no chunks retrieved[/red]")
        return

    console.print()
    for i, c in enumerate(chunks[:3], 1):
        heading = " > ".join(c.heading_path) if c.heading_path else "(no heading)"
        console.print(
            f"  [dim]\\[{i}][/dim] [cyan]{c.doc_title}[/cyan] "
            f"[dim]— {heading}[/dim] "
            f"[yellow]rerank={c.rerank_score:.3f}[/yellow]"
        )
    if len(chunks) > 3:
        console.print(f"  [dim]+ {len(chunks) - 3} more[/dim]")

    if verbose:
        console.print()
        for i, c in enumerate(chunks, 1):
            console.print(f"[dim]--- chunk {i} ---[/dim]")
            console.print(c.content[:600] + ("..." if len(c.content) > 600 else ""))
            console.print()


def _insert_query(
    *,
    user_id: str,
    question: str,
    result_answer: str,
    cited: list[UUID],
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
) -> UUID:
    cost_jpy = (input_tokens * 3 + output_tokens * 15) / 1_000_000 * 150
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO queries
              (user_id, question, answer, cited_chunks, retrieved_chunks,
               latency_ms, input_tokens, output_tokens, cost_jpy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, question, result_answer, cited, cited,
             latency_ms, input_tokens, output_tokens, round(cost_jpy, 4)),
        )
        qid = cur.fetchone()[0]
        c.commit()
        return qid


def _capture_feedback(query_id: UUID) -> None:
    try:
        raw = console.input(
            "  [bold]feedback[/bold] [dim](y/n/Enter or note text)[/dim] ❯ "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return

    if not raw:
        return

    rating: int
    note: str | None
    low = raw.lower()
    if low in ("y", "👍", "yes", "good", "g"):
        rating, note = 1, None
    elif low in ("n", "👎", "no", "bad", "b"):
        rating, note = -1, None
    else:
        rating, note = -1, raw

    with conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE queries SET feedback=%s, feedback_text=%s WHERE id=%s",
            (rating, note, query_id),
        )
        c.commit()

    icon = "👍" if rating == 1 else "👎"
    console.print(f"  [dim]saved {icon}[/dim]")


if __name__ == "__main__":
    app()
