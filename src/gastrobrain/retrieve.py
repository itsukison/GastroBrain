import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from uuid import UUID

from gastrobrain.access import PUBLIC_ONLY, AccessScope
from gastrobrain.config import get_settings
from gastrobrain.db import conn
from gastrobrain.embed import embed_texts, rerank

SLACK_SOURCE = "slack"
# Conversation sources are day-grouped chat transcripts: a matched chunk is a
# slice of a day, so retrieval expands it back to the full day (see
# expand_conversation_parents). Both behave identically downstream.
CONVERSATION_SOURCES = {"slack", "chatwork"}


_Q_FULL = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_Q_JP_FULL = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_Q_JP = re.compile(r"(\d{1,2})月(\d{1,2})日")
_Q_SLASH = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")


def _parse_query_date(question: str) -> date | None:
    """Extract an explicit meeting date from the question, if present. Handles
    YYYY-MM-DD / YYYY年M月D日 (full) and M月D日 / M/D (year inferred: the most
    recent such date not in the future). Returns None when no date is named."""
    def _mk(y: int, mo: int, d: int) -> date | None:
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    m = _Q_FULL.search(question) or _Q_JP_FULL.search(question)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3]))

    m = _Q_JP.search(question) or _Q_SLASH.search(question)
    if m:
        today = date.today()
        cand = _mk(today.year, int(m[1]), int(m[2]))
        if cand and cand > today:
            cand = _mk(today.year - 1, int(m[1]), int(m[2]))
        return cand
    return None


@dataclass
class CandidateChunk:
    chunk_id: UUID
    doc_id: UUID
    doc_title: str
    doc_url: str | None
    heading_path: list[str]
    content: str
    source: str = "notepm"
    updated_at: datetime | None = None


@dataclass
class RetrievedChunk:
    chunk_id: UUID
    doc_id: UUID
    doc_title: str
    doc_url: str | None
    heading_path: list[str]
    content: str
    rerank_score: float
    source: str = "notepm"
    updated_at: datetime | None = None


@dataclass
class RetrievalStats:
    n_dense: int = 0
    n_lexical: int = 0
    n_fused: int = 0
    n_reranked: int = 0
    n_above_floor: int = 0
    elapsed_search_ms: int = 0
    elapsed_rerank_ms: int = 0


# Source-aware visibility gate (see gastrobrain.access / migrations/011, 012).
# A doc is visible when:
#   - NotePM: its note is public, OR the caller's user_code is in the note's set;
#   - Slack:  its channel is public, OR the caller's slack_user_id is a member;
#   - other sources (gdrive/manual): unrestricted.
# The clause is a fixed string with two `%s` (user_code, then slack_user_id);
# both are bound params, so it's injection-safe. A None param matches nothing
# (= NULL is never true), so a caller missing an identity sees only the public
# subset of that source — fail-closed.
_ACCESS_CLAUSE = (
    "AND ((d.source = 'notepm' AND ("
    "d.note_code IN (SELECT note_code FROM notepm_notes WHERE is_public) "
    "OR EXISTS (SELECT 1 FROM notepm_note_access na "
    "WHERE na.note_code = d.note_code AND na.user_code = %s))) "
    "OR (d.source = 'slack' AND ("
    "d.slack_channel_id IN (SELECT channel_id FROM slack_channels WHERE NOT is_private) "
    "OR EXISTS (SELECT 1 FROM slack_channel_access sa "
    "WHERE sa.channel_id = d.slack_channel_id AND sa.slack_user_id = %s))) "
    "OR d.source NOT IN ('notepm', 'slack'))"
)


def access_sql(scope: AccessScope) -> tuple[str, list]:
    """SQL fragment (+ its params) that gates a query over `documents d` by the
    caller's scope. Empty fragment for break-glass scopes. Any query that joins
    `documents d` should use this rather than re-deriving the predicate."""
    if scope.see_all:
        return "", []
    return _ACCESS_CLAUSE, [scope.user_code, scope.slack_user_id]


def retrieve_candidates(
    question: str, stats: RetrievalStats | None = None, scope: AccessScope = PUBLIC_ONLY
) -> list[CandidateChunk]:
    """Dense + lexical search → RRF fusion. Returns top-k_fused candidates.

    `scope` is the caller's access scope (see gastrobrain.access). Only documents
    the scope can see enter the candidate set — restricted docs can't be retrieved,
    reranked, or cited. Defaults to PUBLIC_ONLY so any caller that forgets to pass
    a scope fails closed rather than open. A break-glass scope sees everything."""
    import time

    settings = get_settings()
    t0 = time.perf_counter()
    query_vec = embed_texts([question], input_type="search_query")[0]
    qdate = _parse_query_date(question)

    # see_all → no access clause; otherwise gate by the caller's identities.
    # Param order matches the two `%s` in _ACCESS_CLAUSE (user_code, slack_user_id).
    access_clause, access_params = access_sql(scope)

    def _search(meeting_date: date | None) -> tuple[list, list]:
        # `meeting_date`, when set, restricts both arms to that day's docs.
        # The clause is a fixed string (no user input), so it's injection-safe.
        date_clause = "AND d.meeting_date = %s" if meeting_date else ""
        with conn() as c, c.cursor() as cur:
            dense_params = list(access_params)
            if meeting_date:
                dense_params.append(meeting_date)
            dense_params += [query_vec, settings.retrieve_top_k_dense]
            cur.execute(
                f"""
                SELECT c.id, c.doc_id, d.title, d.url, c.heading_path, c.content, d.source, d.updated_at
                FROM chunks c
                JOIN documents d ON d.id = c.doc_id
                WHERE d.deleted_at IS NULL
                  {access_clause}
                  {date_clause}
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                dense_params,
            )
            dense = cur.fetchall()

            lex_params = list(access_params)
            if meeting_date:
                lex_params.append(meeting_date)
            lex_params += [question, settings.retrieve_top_k_lexical]
            cur.execute(
                f"""
                SELECT c.id, c.doc_id, d.title, d.url, c.heading_path, c.content, d.source, d.updated_at
                FROM chunks c
                JOIN documents d ON d.id = c.doc_id
                WHERE d.deleted_at IS NULL
                  {access_clause}
                  {date_clause}
                  AND c.content &@~ %s
                ORDER BY pgroonga_score(c.tableoid, c.ctid) DESC
                LIMIT %s
                """,
                lex_params,
            )
            lexical = cur.fetchall()
        return dense, lexical

    dense, lexical = _search(qdate)
    fused = _rrf_fuse(dense, lexical, k=60, top_n=settings.retrieve_top_k_fused)
    if qdate and not fused:
        # Query named a date but nothing matched it (no meeting that day, or a
        # false-positive parse like "2/3 のユーザー"). Fall back to an unfiltered
        # search rather than returning nothing.
        dense, lexical = _search(None)
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
            heading_path=row[4] or [], content=row[5], source=row[6], updated_at=row[7],
        )
        for row in fused
    ]


# Recency tie-break authority weights. Newer ≠ more authoritative: a Slack day's
# updated_at (last message) is always "fresher" than a curated NotePM page edited
# months ago, so Slack gets a fraction of the recency bonus — it can only win a
# genuine near-tie, never leapfrog an authoritative doc on relevance.
_SOURCE_AUTHORITY = {"notepm": 1.0, "gdrive": 1.0, "manual": 1.0, "slack": 0.3, "chatwork": 0.3}


def _recency_bonus(updated_at: datetime | None, source: str, now: datetime, settings) -> float:
    """Bounded recency bonus in [0, recency_max_bonus]. Exponential decay on age
    (newest ≈ max_bonus, old → 0), scaled by source authority. Returns 0 when the
    timestamp is missing."""
    if updated_at is None:
        return 0.0
    age_days = max(0.0, (now - updated_at).total_seconds() / 86400)
    decay = 0.5 ** (age_days / settings.recency_halflife_days)
    authority = _SOURCE_AUTHORITY.get(source, 1.0)
    return settings.recency_max_bonus * authority * decay


def rerank_candidates(
    question: str, candidates: list[CandidateChunk], stats: RetrievalStats | None = None
) -> list[RetrievedChunk]:
    """Cohere rerank over candidates, then a bounded recency tie-break. Returns
    top-k_final above the score floor.

    The floor is applied on the *base* rerank score (relevance only), so a fresh
    doc can never be rescued past the floor by recency. We over-fetch a few extra
    so the tie-break can pull a recent doc into the final top-k; the returned
    rerank_score is the base relevance score, not the boosted one."""
    import time

    settings = get_settings()
    t0 = time.perf_counter()
    if not candidates:
        if stats is not None:
            stats.elapsed_rerank_ms = 0
        return []

    docs = [c.content for c in candidates]
    over_n = min(len(docs), settings.retrieve_top_k_final + settings.rerank_overfetch)
    rerank_results = rerank(question, docs, top_n=over_n)
    now = datetime.now(timezone.utc)

    scored: list[tuple[float, float, CandidateChunk]] = []
    for idx, score in rerank_results:
        if score < settings.rerank_score_floor:
            continue
        c = candidates[idx]
        combined = score + _recency_bonus(c.updated_at, c.source, now, settings)
        scored.append((combined, score, c))

    scored.sort(key=lambda t: t[0], reverse=True)
    out = [
        RetrievedChunk(
            chunk_id=c.chunk_id, doc_id=c.doc_id, doc_title=c.doc_title,
            doc_url=c.doc_url, heading_path=c.heading_path,
            content=c.content, rerank_score=base, source=c.source, updated_at=c.updated_at,
        )
        for _combined, base, c in scored[: settings.retrieve_top_k_final]
    ]

    if stats is not None:
        stats.n_reranked = len(rerank_results)
        stats.n_above_floor = len(out)
        stats.elapsed_rerank_ms = int((time.perf_counter() - t0) * 1000)

    return out


def _fetch_raw_markdown(doc_ids: set[UUID]) -> dict[UUID, str]:
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT id, raw_markdown FROM documents WHERE id = ANY(%s)", (list(doc_ids),))
        return {row[0]: row[1] for row in cur.fetchall()}


def expand_conversation_parents(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Small-to-big for chat sources (Slack, Chatwork). A matched chunk is a
    ~500-char slice of a day's conversation, but the answer ("what did we
    conclude") may live in another part of that day. Replace each such chunk with
    its full parent conversation (documents.raw_markdown), keeping only the
    best-ranked chunk per doc so the day appears once. Other sources are
    untouched. Idempotent: re-running on already-expanded chunks is a no-op."""
    conv_ids = {c.doc_id for c in chunks if c.source in CONVERSATION_SOURCES}
    if not conv_ids:
        return chunks
    bodies = _fetch_raw_markdown(conv_ids)
    out: list[RetrievedChunk] = []
    seen: set[UUID] = set()
    for c in chunks:
        if c.source not in CONVERSATION_SOURCES:
            out.append(c)
            continue
        if c.doc_id in seen:
            continue
        seen.add(c.doc_id)
        out.append(replace(c, content=bodies.get(c.doc_id, c.content), heading_path=[]))
    return out


def retrieve(question: str, scope: AccessScope = PUBLIC_ONLY) -> list[RetrievedChunk]:
    """Convenience wrapper: candidates + rerank in one call. Used by the CLI and
    the MCP surface. `scope` gates which documents are searchable."""
    candidates = retrieve_candidates(question, scope=scope)
    return expand_conversation_parents(rerank_candidates(question, candidates))


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
