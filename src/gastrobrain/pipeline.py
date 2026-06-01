"""Surface-agnostic orchestrator.

`run_pipeline` is the single source of truth for the question→answer flow.
Slack and the web API both consume the same event stream and translate it
into their respective surface idioms. Adding a new surface (e.g. CLI, REST
synchronous) should not require touching this module."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from gastrobrain.generate import (
    HistoryTurn,
    StreamDelta,
    StreamDone,
    UserPreferences,
    answer_stream,
)
from gastrobrain.retrieve import (
    RetrievalStats,
    RetrievedChunk,
    rerank_candidates,
    retrieve_candidates,
)
from gastrobrain.rewrite import standalone_query

log = logging.getLogger("gastrobrain.pipeline")

Surface = Literal["slack", "web"]


@dataclass
class QueryRewritten:
    original: str
    rewritten: str


@dataclass
class RetrievalStarted:
    pass


@dataclass
class RetrievalDone:
    n_candidates: int
    stats: RetrievalStats


@dataclass
class RerankDone:
    chunks: list[RetrievedChunk]
    stats: RetrievalStats


@dataclass
class AnswerToken:
    text: str


@dataclass
class AnswerDone:
    answer: str
    chunks: list[RetrievedChunk]
    stats: RetrievalStats
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_jpy: float


PipelineEvent = (
    QueryRewritten
    | RetrievalStarted
    | RetrievalDone
    | RerankDone
    | AnswerToken
    | AnswerDone
)


# Sonnet 4.6 pricing (USD per MTok), converted at ¥150/USD. Cache reads at 10% of input.
_USD_JPY = 150
_INPUT_PER_MTOK_USD = 3.0
_OUTPUT_PER_MTOK_USD = 15.0
_CACHE_READ_DISCOUNT = 0.1


def _cost_jpy(input_tokens: int, output_tokens: int, cache_read: int) -> float:
    billed_input = max(input_tokens - cache_read, 0)
    cost_usd = (
        billed_input * _INPUT_PER_MTOK_USD
        + cache_read * _INPUT_PER_MTOK_USD * _CACHE_READ_DISCOUNT
        + output_tokens * _OUTPUT_PER_MTOK_USD
    ) / 1_000_000
    return round(cost_usd * _USD_JPY, 4)


@dataclass
class PipelineInput:
    question: str
    user_id: str
    history: list[HistoryTurn] = field(default_factory=list)
    surface: Surface = "web"
    prefs: UserPreferences | None = None
    # Caller's clearance level (gastrobrain.access). Gates which documents are
    # searchable. Defaults to 0 = unrestricted-only (fail-closed).
    max_level: int = 0


async def run_pipeline(inp: PipelineInput) -> AsyncIterator[PipelineEvent]:
    """Async generator that drives the full pipeline and emits structured events.

    Stages:
      1. Optional Haiku rewrite (only when history is non-empty).
      2. Hybrid retrieval (BM25 + dense + RRF).
      3. Rerank.
      4. Streaming generation.

    Each blocking call is dispatched via asyncio.to_thread so the SSE response
    stays responsive."""
    t0 = time.perf_counter()
    stats = RetrievalStats()

    # 1. Query rewrite (only when there's prior context)
    retrieval_query = inp.question
    if inp.history:
        rewritten = await asyncio.to_thread(standalone_query, inp.question, inp.history)
        if rewritten and rewritten != inp.question:
            retrieval_query = rewritten
            yield QueryRewritten(original=inp.question, rewritten=rewritten)

    # 2. Retrieval
    yield RetrievalStarted()
    candidates = await asyncio.to_thread(
        retrieve_candidates, retrieval_query, stats, inp.max_level
    )
    yield RetrievalDone(n_candidates=len(candidates), stats=stats)

    # 3. Rerank
    chunks: list[RetrievedChunk] = []
    if candidates:
        chunks = await asyncio.to_thread(rerank_candidates, retrieval_query, candidates, stats)
    yield RerankDone(chunks=chunks, stats=stats)

    # 4. Streaming generation
    buf: list[str] = []
    usage = StreamDone(answer="", input_tokens=0, output_tokens=0,
                       cache_read_input_tokens=0, cache_creation_input_tokens=0)

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def _drive():
        try:
            for ev in answer_stream(
                inp.question, chunks, inp.history,
                surface=inp.surface, prefs=inp.prefs,
            ):
                queue.put_nowait(ev)
        except Exception as e:  # noqa: BLE001
            queue.put_nowait(e)
        finally:
            queue.put_nowait(sentinel)

    task = asyncio.create_task(asyncio.to_thread(_drive))
    while True:
        ev = await queue.get()
        if ev is sentinel:
            break
        if isinstance(ev, Exception):
            await task
            raise ev
        if isinstance(ev, StreamDelta):
            buf.append(ev.text)
            yield AnswerToken(text=ev.text)
        elif isinstance(ev, StreamDone):
            usage = ev
    await task

    latency_ms = int((time.perf_counter() - t0) * 1000)
    cost_jpy = _cost_jpy(usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens)
    yield AnswerDone(
        answer="".join(buf) if buf else usage.answer,
        chunks=chunks,
        stats=stats,
        latency_ms=latency_ms,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cost_jpy=cost_jpy,
    )


# --------------------------------------------------------------------------------------
# Slack-compatible adapter — preserves the existing `_process_question` callable shape.
# --------------------------------------------------------------------------------------


async def run_pipeline_for_slack(
    *,
    question: str,
    user_id: str,
    update,
    insert_query,
    build_answer_blocks,
    max_level: int = 0,
) -> UUID | None:
    """Drives `run_pipeline` and maps events to the Slack update() callback.

    `max_level` is the Slack user's resolved clearance level (slack_app.py does
    the Slack-id→email→level lookup). Returns the inserted query_id (or None if
    persistence failed)."""
    inp = PipelineInput(
        question=question, user_id=user_id, history=[], surface="slack", max_level=max_level
    )

    chunks: list[RetrievedChunk] = []
    final: AnswerDone | None = None

    async for ev in run_pipeline(inp):
        if isinstance(ev, RetrievalDone):
            if ev.n_candidates == 0:
                await update("ℹ️ 関連情報が見つかりませんでした。")
            else:
                await update(f"🔍 {ev.stats.n_fused}件の候補から関連性を評価中...")
        elif isinstance(ev, RerankDone):
            chunks = ev.chunks
            if chunks:
                await update(f"✍️ {ev.stats.n_above_floor}件の関連情報から回答を生成中...")
            elif ev.stats.n_fused:
                await update("ℹ️ 質問に十分関連する情報が見つかりませんでした。")
        elif isinstance(ev, AnswerDone):
            final = ev

    if final is None:
        log.error("pipeline ended without AnswerDone for user=%s", user_id)
        return None

    try:
        qid = await asyncio.to_thread(
            insert_query,
            user_id=user_id,
            question=question,
            result_answer=final.answer,
            cited=[c.chunk_id for c in chunks],
            latency_ms=final.latency_ms,
            input_tokens=final.input_tokens,
            output_tokens=final.output_tokens,
        )
    except Exception:
        log.exception("failed to insert query row")
        qid = None

    blocks = build_answer_blocks(final.answer, chunks, qid, final.latency_ms, final.stats)
    await update(text=final.answer[:300] or "(no answer)", blocks=blocks, final=True)
    return qid
