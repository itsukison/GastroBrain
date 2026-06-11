import time

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from gastrobrain.access import SEE_ALL
from gastrobrain.db import conn
from gastrobrain.generate import answer
from gastrobrain.retrieve import retrieve

app = typer.Typer(add_completion=False, help="Ask the corpus a question")
console = Console()


@app.command()
def main(
    question: str = typer.Argument(..., help="Question in Japanese (or English)"),
    user: str = typer.Option("cli", help="User identifier for query log"),
    show_chunks: bool = typer.Option(False, "--show-chunks", help="Print retrieved chunks"),
) -> None:
    t0 = time.perf_counter()
    chunks = retrieve(question, scope=SEE_ALL)
    t_retrieve = time.perf_counter() - t0

    if show_chunks:
        for i, c in enumerate(chunks, 1):
            heading = " > ".join(c.heading_path) if c.heading_path else "(no heading)"
            console.print(
                Panel(
                    c.content[:400] + ("..." if len(c.content) > 400 else ""),
                    title=f"[{i}] {c.doc_title} — {heading} (rerank={c.rerank_score:.3f})",
                    border_style="dim",
                )
            )

    t1 = time.perf_counter()
    result = answer(question, chunks)
    t_generate = time.perf_counter() - t1
    t_total = time.perf_counter() - t0

    console.print()
    console.print(Markdown(result.answer))
    console.print()
    console.print(
        f"[dim]retrieve={t_retrieve*1000:.0f}ms  generate={t_generate*1000:.0f}ms  "
        f"total={t_total*1000:.0f}ms  in={result.input_tokens}  out={result.output_tokens}  "
        f"cache_read={result.cache_read_input_tokens}[/dim]"
    )

    _log_query(
        user_id=user,
        question=question,
        result_answer=result.answer,
        cited=[c.chunk_id for c in chunks],
        latency_ms=int(t_total * 1000),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def _log_query(
    user_id: str,
    question: str,
    result_answer: str,
    cited: list,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    cost_jpy = (
        input_tokens * 3 / 1_000_000 * 150
        + output_tokens * 15 / 1_000_000 * 150
    )
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO queries
              (user_id, question, answer, cited_chunks, retrieved_chunks,
               latency_ms, input_tokens, output_tokens, cost_jpy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, question, result_answer, cited, cited,
             latency_ms, input_tokens, output_tokens, round(cost_jpy, 4)),
        )
        c.commit()


if __name__ == "__main__":
    app()
