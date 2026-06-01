from dataclasses import dataclass, field
from uuid import UUID

from gastrobrain.config import get_settings
from gastrobrain.db import conn
from gastrobrain.embed import embed_texts, rerank


@dataclass
class CandidateChunk:
    chunk_id: UUID
    doc_id: UUID
    doc_title: str
    doc_url: str | None
    heading_path: list[str]
    content: str


@dataclass
class RetrievedChunk:
    chunk_id: UUID
    doc_id: UUID
    doc_title: str
    doc_url: str | None
    heading_path: list[str]
    content: str
    rerank_score: float


@dataclass
class RetrievalStats:
    n_dense: int = 0
    n_lexical: int = 0
    n_fused: int = 0
    n_reranked: int = 0
    n_above_floor: int = 0
    elapsed_search_ms: int = 0
    elapsed_rerank_ms: int = 0


def retrieve_candidates(
    question: str, stats: RetrievalStats | None = None, max_level: int = 0
) -> list[CandidateChunk]:
    """Dense + lexical search → RRF fusion. Returns top-k_fused candidates.

    `max_level` is the caller's clearance level (see gastrobrain.access). Only
    documents with `min_level <= max_level` are searched, so restricted docs
    never enter the candidate set — they can't be retrieved, reranked, or cited.
    Defaults to 0 (unrestricted-only) so any caller that forgets to pass a level
    fails closed rather than open."""
    import time

    settings = get_settings()
    t0 = time.perf_counter()
    query_vec = embed_texts([question], input_type="search_query")[0]

    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.doc_id, d.title, d.url, c.heading_path, c.content
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id
            WHERE d.deleted_at IS NULL
              AND d.min_level <= %s
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (max_level, query_vec, settings.retrieve_top_k_dense),
        )
        dense = cur.fetchall()

        cur.execute(
            """
            SELECT c.id, c.doc_id, d.title, d.url, c.heading_path, c.content
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id
            WHERE d.deleted_at IS NULL
              AND d.min_level <= %s
              AND c.content &@~ %s
            ORDER BY pgroonga_score(c.tableoid, c.ctid) DESC
            LIMIT %s
            """,
            (max_level, question, settings.retrieve_top_k_lexical),
        )
        lexical = cur.fetchall()

    fused = _rrf_fuse(dense, lexical, k=60, top_n=settings.retrieve_top_k_fused)
    elapsed = int((time.perf_counter() - t0) * 1000)

    if stats is not None:
        stats.n_dense = len(dense)
        stats.n_lexical = len(lexical)
        stats.n_fused = len(fused)
        stats.elapsed_search_ms = elapsed

    return [
        CandidateChunk(
            chunk_id=row[0], doc_id=row[1], doc_title=row[2], doc_url=row[3],
            heading_path=row[4] or [], content=row[5],
        )
        for row in fused
    ]


def rerank_candidates(
    question: str, candidates: list[CandidateChunk], stats: RetrievalStats | None = None
) -> list[RetrievedChunk]:
    """Cohere rerank over candidates. Returns top-k_final above the score floor."""
    import time

    settings = get_settings()
    t0 = time.perf_counter()
    if not candidates:
        if stats is not None:
            stats.elapsed_rerank_ms = 0
        return []

    docs = [c.content for c in candidates]
    rerank_results = rerank(question, docs, top_n=settings.retrieve_top_k_final)
    out: list[RetrievedChunk] = []
    for idx, score in rerank_results:
        if score < settings.rerank_score_floor:
            break
        c = candidates[idx]
        out.append(
            RetrievedChunk(
                chunk_id=c.chunk_id, doc_id=c.doc_id, doc_title=c.doc_title,
                doc_url=c.doc_url, heading_path=c.heading_path,
                content=c.content, rerank_score=score,
            )
        )

    if stats is not None:
        stats.n_reranked = len(rerank_results)
        stats.n_above_floor = len(out)
        stats.elapsed_rerank_ms = int((time.perf_counter() - t0) * 1000)

    return out


def retrieve(question: str, max_level: int = 0) -> list[RetrievedChunk]:
    """Convenience wrapper: candidates + rerank in one call. Used by the CLI and
    the MCP surface. `max_level` gates which documents are searchable."""
    candidates = retrieve_candidates(question, max_level=max_level)
    return rerank_candidates(question, candidates)


def _rrf_fuse(dense: list[tuple], lexical: list[tuple], k: int, top_n: int) -> list[tuple]:
    """Reciprocal Rank Fusion. Returns top_n unique rows by chunk_id."""
    scores: dict = {}
    rows: dict = {}

    for rank, row in enumerate(dense):
        cid = row[0]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
        rows.setdefault(cid, row)

    for rank, row in enumerate(lexical):
        cid = row[0]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
        rows.setdefault(cid, row)

    ranked_ids = sorted(scores, key=scores.get, reverse=True)[:top_n]
    return [rows[cid] for cid in ranked_ids]
