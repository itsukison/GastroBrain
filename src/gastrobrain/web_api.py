"""Web API router — Next.js (and any other future surface) talks to this.

Mounted onto the same FastAPI app as the Slack handler. Endpoints under /v1/*.
Auth: Supabase HS256 JWT via the `require_user` dependency. Ownership is
enforced at the SQL layer (every query filters on user_id = $1); the RLS
policies in 002_web_chat.sql are defense-in-depth for direct DB access.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from gastrobrain import llm
from gastrobrain.access import (
    PUBLIC_ONLY,
    AccessScope,
    is_admin,
    recompute_document_levels,
    resolve_access,
)
from gastrobrain.auth import AuthUser, require_user
from gastrobrain.config import get_settings
from gastrobrain.db import conn
from gastrobrain.generate import HistoryTurn, UserPreferences
from gastrobrain.meeting_qa import format_meeting_record

_ALLOWED_DEPARTMENTS = {"consulting", "sales", "content", "dev", "backoffice", "other"}
from gastrobrain.pipeline import (
    AnswerDone,
    AnswerToken,
    PipelineInput,
    QueryRewritten,
    RerankDone,
    RetrievalDone,
    RetrievalStarted,
    run_pipeline,
)
from gastrobrain.retrieve import RetrievedChunk, access_sql
from gastrobrain.slack_format import assign_source_numbers

log = logging.getLogger("gastrobrain.web_api")

router = APIRouter(prefix="/v1")


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class ThreadCreateBody(BaseModel):
    title: str | None = None
    # Set when the thread is "ask a question about this meeting" (§4). The
    # caller must be a participant; the meeting's transcript then rides along
    # into every turn of this thread.
    meeting_id: UUID | None = None


class ThreadPatchBody(BaseModel):
    title: str | None = None
    archived: bool | None = None


class ChatBody(BaseModel):
    conversation_id: UUID
    question: str = Field(min_length=1, max_length=4000)


class FeedbackBody(BaseModel):
    rating: int = Field(ge=-1, le=1)
    text: str | None = Field(default=None, max_length=2000)


class VoiceAskBody(BaseModel):
    conversation_id: UUID
    # Shorter cap than /chat: this arrives from a speech transcript, and a
    # 4,000-char "question" means the transcriber ran away.
    question: str = Field(min_length=1, max_length=1000)
    # What the person actually said, as opposed to the context-expanded query
    # above. Stored as the `messages` row so the thread is a readable record of
    # the conversation. Optional: an older client that omits it still works.
    utterance: str | None = Field(default=None, max_length=1000)


class VoiceAnswerOut(BaseModel):
    answer: str
    citations: list[dict]
    message_id: UUID
    query_id: UUID | None
    latency_ms: int


class VoiceVocabOut(BaseModel):
    terms: list[str]


class PreferencesBody(BaseModel):
    # department=None clears the setting. Anything other than the allowed enum
    # is rejected at request time (not silently stored as NULL).
    department: str | None = None
    # extra_note=None or "" clears the note. 300字 上限は DB CHECK でも担保。
    extra_note: str | None = Field(default=None, max_length=300)


class PreferencesOut(BaseModel):
    department: str | None
    extra_note: str | None
    updated_at: str | None


class ThreadOut(BaseModel):
    id: UUID
    title: str
    created_at: str
    updated_at: str
    archived_at: str | None
    last_message_preview: str | None = None


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    created_at: str
    citations: list[dict] | None = None
    query_id: UUID | None = None
    feedback: int | None = None


# --------------------------------------------------------------------------------------
# Threads CRUD
# --------------------------------------------------------------------------------------


@router.post("/threads", response_model=ThreadOut)
async def create_thread(body: ThreadCreateBody, user: AuthUser = Depends(require_user)) -> ThreadOut:
    def _do() -> dict:
        with conn() as c, c.cursor() as cur:
            if body.meeting_id is not None:
                _require_participant(cur, body.meeting_id, user.email)
            cur.execute(
                """
                INSERT INTO conversations (user_id, title, meeting_id)
                VALUES (%s, COALESCE(%s, '新規チャット'), %s)
                RETURNING id, title, created_at, updated_at, archived_at
                """,
                (str(user.user_id), body.title, str(body.meeting_id) if body.meeting_id else None),
            )
            row = cur.fetchone()
            c.commit()
            return _row_to_thread(row)

    return ThreadOut(**await asyncio.to_thread(_do))


@router.get("/threads")
async def list_threads(
    limit: int = 50,
    cursor: str | None = None,
    archived: bool = False,
    user: AuthUser = Depends(require_user),
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            params: list[Any] = [str(user.user_id)]
            where = ["user_id = %s", "deleted_at IS NULL"]
            if archived:
                where.append("archived_at IS NOT NULL")
            else:
                where.append("archived_at IS NULL")
            if cursor:
                where.append("updated_at < %s")
                params.append(cursor)
            params.append(limit + 1)
            cur.execute(
                f"""
                SELECT id, title, created_at, updated_at, archived_at
                FROM conversations
                WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
            next_cursor: str | None = None
            if len(rows) > limit:
                next_cursor = rows[limit - 1][3].isoformat()
                rows = rows[:limit]
            return {
                "threads": [_row_to_thread(r) for r in rows],
                "next_cursor": next_cursor,
            }

    return await asyncio.to_thread(_do)


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: UUID, user: AuthUser = Depends(require_user)) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, created_at, updated_at, archived_at
                FROM conversations
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (str(thread_id), str(user.user_id)),
            )
            conv = cur.fetchone()
            if not conv:
                raise HTTPException(status_code=404, detail="thread not found")

            cur.execute(
                """
                SELECT m.id, m.role, m.content, m.created_at, m.cited_chunks,
                       m.query_id, q.feedback
                FROM messages m
                LEFT JOIN queries q ON q.id = m.query_id
                WHERE m.conversation_id = %s
                ORDER BY m.created_at ASC
                """,
                (str(thread_id),),
            )
            msg_rows = cur.fetchall()

            chunk_ids: set[UUID] = set()
            for r in msg_rows:
                for cid in r[4] or []:
                    chunk_ids.add(cid)

            chunks_by_id: dict[UUID, dict] = {}
            if chunk_ids:
                cur.execute(
                    """
                    SELECT c.id, c.heading_path, c.content, d.title, d.url
                    FROM chunks c
                    JOIN documents d ON d.id = c.doc_id
                    WHERE c.id = ANY(%s)
                    """,
                    (list(chunk_ids),),
                )
                for row in cur.fetchall():
                    chunks_by_id[row[0]] = {
                        "chunk_id": str(row[0]),
                        "heading_path": row[1] or [],
                        "snippet": (row[2] or "")[:240],
                        "doc_title": row[3],
                        "doc_url": row[4],
                    }

            messages_out: list[dict] = []
            for r in msg_rows:
                cited = r[4] or []
                citations: list[dict] | None = None
                if cited:
                    snapshot = [chunks_by_id[cid] for cid in cited if cid in chunks_by_id]
                    citations = _shape_citations(snapshot)
                messages_out.append(
                    {
                        "id": str(r[0]),
                        "role": r[1],
                        "content": r[2],
                        "created_at": r[3].isoformat(),
                        "citations": citations,
                        "query_id": str(r[5]) if r[5] else None,
                        "feedback": r[6],
                    }
                )

            return {
                "thread": _row_to_thread(conv),
                "messages": messages_out,
            }

    return await asyncio.to_thread(_do)


@router.patch("/threads/{thread_id}", response_model=ThreadOut)
async def patch_thread(
    thread_id: UUID,
    body: ThreadPatchBody,
    user: AuthUser = Depends(require_user),
) -> ThreadOut:
    if body.title is None and body.archived is None:
        raise HTTPException(status_code=400, detail="no fields to update")

    def _do() -> dict:
        sets: list[str] = []
        params: list[Any] = []
        if body.title is not None:
            sets.append("title = %s")
            params.append(body.title.strip() or "新規チャット")
        if body.archived is True:
            sets.append("archived_at = now()")
        elif body.archived is False:
            sets.append("archived_at = NULL")
        params.extend([str(thread_id), str(user.user_id)])
        with conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                UPDATE conversations
                SET {', '.join(sets)}, updated_at = now()
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                RETURNING id, title, created_at, updated_at, archived_at
                """,
                params,
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="thread not found")
            c.commit()
            return _row_to_thread(row)

    return ThreadOut(**await asyncio.to_thread(_do))


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: UUID, user: AuthUser = Depends(require_user)) -> None:
    def _do() -> None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE conversations
                SET deleted_at = now()
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (str(thread_id), str(user.user_id)),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="thread not found")
            c.commit()

    await asyncio.to_thread(_do)


# --------------------------------------------------------------------------------------
# Auto-title (Haiku, one shot)
# --------------------------------------------------------------------------------------


_TITLE_SYSTEM = """あなたは社内ナレッジQ&Aツールの会話タイトル生成器です。

ルール:
1. ユーザーの最初の質問とアシスタントの最初の回答（抜粋）から、その会話の内容を表す日本語タイトルを1つ生成する。
2. 出力はタイトル本文1行のみ。記号・引用符・前置きは付けない。
3. 長さは全角14文字以内を目安に簡潔にまとめる。
4. 固有名詞・キーワードを優先する。汎用的な「質問について」などは避ける。"""


@router.post("/threads/{thread_id}/title", response_model=ThreadOut)
async def generate_title(thread_id: UUID, user: AuthUser = Depends(require_user)) -> ThreadOut:
    """Run a Haiku call over the first user+assistant turn to set a meaningful title.
    Called by the client after the first answer streams to done."""
    def _load() -> tuple[dict, str, str]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, created_at, updated_at, archived_at
                FROM conversations
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (str(thread_id), str(user.user_id)),
            )
            conv = cur.fetchone()
            if not conv:
                raise HTTPException(status_code=404, detail="thread not found")

            cur.execute(
                """
                SELECT role, content FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at ASC
                LIMIT 2
                """,
                (str(thread_id),),
            )
            rows = cur.fetchall()
            q = next((r[1] for r in rows if r[0] == "user"), "")
            a = next((r[1] for r in rows if r[0] == "assistant"), "")
            return _row_to_thread(conv), q, a

    conv, question, answer_text = await asyncio.to_thread(_load)
    if not question:
        return ThreadOut(**conv)

    user_msg = (
        f"質問: {question.strip()[:400]}\n"
        f"回答(抜粋): {answer_text.strip()[:400]}\n\n"
        "上記の会話のタイトルを生成してください。"
    )
    try:
        resp = await asyncio.to_thread(
            lambda: llm.complete(
                system=_TITLE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=64,
                mini=True,
            )
        )
        title = resp.text.strip()
        title = title.strip("「」\"' \n")[:60] or conv["title"]
    except Exception:
        log.exception("title generation failed; keeping default")
        return ThreadOut(**conv)

    def _save() -> dict:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE conversations
                SET title = %s
                WHERE id = %s AND user_id = %s
                RETURNING id, title, created_at, updated_at, archived_at
                """,
                (title, str(thread_id), str(user.user_id)),
            )
            row = cur.fetchone()
            c.commit()
            return _row_to_thread(row)

    return ThreadOut(**await asyncio.to_thread(_save))


# --------------------------------------------------------------------------------------
# Chat (SSE)
# --------------------------------------------------------------------------------------


@router.post("/chat")
async def chat(body: ChatBody, user: AuthUser = Depends(require_user)) -> EventSourceResponse:
    """SSE endpoint. The client receives a stream of JSON-encoded events.

    Event types: `query_rewritten`, `retrieval_started`, `retrieval_done`,
    `rerank_done`, `token`, `citations`, `done`, `error`.
    """
    log.info("chat: prep start conversation=%s user=%s", body.conversation_id, user.user_id)
    history, _, prefs, scope, meeting_context = await asyncio.to_thread(
        _prep_turn,
        conversation_id=body.conversation_id,
        user=user,
        question=body.question,
    )
    log.info("chat: prep done history_len=%d prefs=%s scope=%s meeting=%s",
             len(history), prefs, scope, meeting_context is not None)

    async def _events():
        # Emit immediately so the client can distinguish "Cloud Run accepted +
        # SSE flushing works" from "pipeline silently hung before any yield".
        yield _sse("pipeline_started", {"ts": time.time()})
        try:
            inp = PipelineInput(
                question=body.question,
                user_id=str(user.user_id),
                history=history,
                surface="web",
                prefs=prefs,
                scope=scope,
                extra_context=meeting_context,
            )
            final: AnswerDone | None = None
            token_count = 0
            async for ev in run_pipeline(inp):
                if isinstance(ev, QueryRewritten):
                    log.info("chat: query_rewritten")
                    yield _sse("query_rewritten",
                               {"original": ev.original, "rewritten": ev.rewritten})
                elif isinstance(ev, RetrievalStarted):
                    log.info("chat: retrieval_started")
                    yield _sse("retrieval_started", {})
                elif isinstance(ev, RetrievalDone):
                    log.info("chat: retrieval_done n_candidates=%d", ev.n_candidates)
                    yield _sse("retrieval_done", {"n_candidates": ev.n_candidates})
                elif isinstance(ev, RerankDone):
                    log.info("chat: rerank_done n_chunks=%d", len(ev.chunks))
                    yield _sse("rerank_done",
                               {"n_chunks": len(ev.chunks),
                                "citations": _shape_citations_from_chunks(ev.chunks)})
                elif isinstance(ev, AnswerToken):
                    token_count += 1
                    yield _sse("token", {"text": ev.text})
                elif isinstance(ev, AnswerDone):
                    log.info("chat: answer_done tokens=%d output_tokens=%d latency_ms=%d",
                             token_count, ev.output_tokens, ev.latency_ms)
                    final = ev

            if final is None:
                log.warning("chat: pipeline ended without answer (tokens streamed=%d)", token_count)
                yield _sse("error", {"message": "pipeline ended without answer"})
                return

            message_id, query_id = await asyncio.to_thread(
                _persist_assistant_turn,
                conversation_id=body.conversation_id,
                user_id=user.user_id,
                question=body.question,
                final=final,
            )
            yield _sse(
                "done",
                {
                    "message_id": str(message_id),
                    "query_id": str(query_id) if query_id else None,
                    "latency_ms": final.latency_ms,
                    "input_tokens": final.input_tokens,
                    "output_tokens": final.output_tokens,
                    "cost_jpy": final.cost_jpy,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("chat stream failed")
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"[:500]})

    return EventSourceResponse(_events(), media_type="text/event-stream")


# --------------------------------------------------------------------------------------
# Voice (non-streaming) — the "supervisor" behind the Realtime voice agent.
#
# The browser's Realtime session calls this as a function tool. It is the same
# pipeline the web chat runs, with surface="voice" (spoken formatting, hard
# length cap) and no SSE: the voice agent can't speak a partial answer, so the
# whole turn is one request/response. See docs/VOICE_AGENT_PLAN.md.
# --------------------------------------------------------------------------------------


@router.post("/voice/ask", response_model=VoiceAnswerOut)
async def voice_ask(body: VoiceAskBody, user: AuthUser = Depends(require_user)) -> VoiceAnswerOut:
    log.info("voice/ask: start conversation=%s user=%s", body.conversation_id, user.user_id)
    history, _, prefs, scope, meeting_context = await asyncio.to_thread(
        _prep_turn,
        conversation_id=body.conversation_id,
        user=user,
        question=body.question,
        stored_question=(body.utterance or "").strip() or None,
    )

    inp = PipelineInput(
        question=body.question,
        user_id=str(user.user_id),
        history=history,
        surface="voice",
        prefs=prefs,
        scope=scope,
        extra_context=meeting_context,
    )
    final: AnswerDone | None = None
    async for ev in run_pipeline(inp):
        if isinstance(ev, AnswerDone):
            final = ev
    if final is None:
        raise HTTPException(status_code=502, detail="pipeline ended without answer")

    message_id, query_id = await asyncio.to_thread(
        _persist_assistant_turn,
        conversation_id=body.conversation_id,
        user_id=user.user_id,
        question=body.question,
        final=final,
    )
    log.info(
        "voice/ask: done chars=%d citations=%d latency_ms=%d",
        len(final.answer), len(final.chunks), final.latency_ms,
    )
    return VoiceAnswerOut(
        answer=final.answer,
        citations=_shape_citations_from_chunks(final.chunks),
        message_id=message_id,
        query_id=query_id,
        latency_ms=final.latency_ms,
    )


@router.get("/voice/vocab", response_model=VoiceVocabOut)
async def voice_vocab(user: AuthUser = Depends(require_user)) -> VoiceVocabOut:
    """Domain proper nouns, fed to the Realtime session's transcription model as
    a decoding hint. Store names like 福栄組合 and notebook titles are exactly what
    a general-purpose ASR mangles, and a mangled noun becomes a failed retrieval.

    Scoped to what this user may see, so the hint list can't leak the existence
    of a document they have no access to."""
    terms = await asyncio.to_thread(_voice_vocab_terms, user.email)
    return VoiceVocabOut(terms=terms)


# --------------------------------------------------------------------------------------
# User preferences (web-only — applied to system prompt in generate.system_prompt)
# --------------------------------------------------------------------------------------


@router.get("/preferences", response_model=PreferencesOut)
async def get_preferences(user: AuthUser = Depends(require_user)) -> PreferencesOut:
    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT department, extra_note, updated_at FROM user_preferences WHERE user_id = %s",
                (str(user.user_id),),
            )
            row = cur.fetchone()
            if not row:
                return {"department": None, "extra_note": None, "updated_at": None}
            return {
                "department": row[0],
                "extra_note": row[1],
                "updated_at": row[2].isoformat() if row[2] else None,
            }

    return PreferencesOut(**await asyncio.to_thread(_do))


@router.put("/preferences", response_model=PreferencesOut)
async def put_preferences(
    body: PreferencesBody,
    user: AuthUser = Depends(require_user),
) -> PreferencesOut:
    if body.department is not None and body.department not in _ALLOWED_DEPARTMENTS:
        raise HTTPException(status_code=400, detail="invalid department")

    # Treat "" the same as None — keeps the DB tidy and the prompt block empty.
    note = body.extra_note.strip() if body.extra_note else None
    if note == "":
        note = None

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_preferences (user_id, department, extra_note, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE
                    SET department = EXCLUDED.department,
                        extra_note = EXCLUDED.extra_note,
                        updated_at = now()
                RETURNING department, extra_note, updated_at
                """,
                (str(user.user_id), body.department, note),
            )
            row = cur.fetchone()
            c.commit()
            return {
                "department": row[0],
                "extra_note": row[1],
                "updated_at": row[2].isoformat() if row[2] else None,
            }

    return PreferencesOut(**await asyncio.to_thread(_do))


# --------------------------------------------------------------------------------------
# MCP tokens (self-service — logged-in users can mint their own bearer tokens
# for the /mcp/ endpoint without bothering an admin)
# --------------------------------------------------------------------------------------


class McpTokenOut(BaseModel):
    """A token row returned to the owner. `token` is populated only on the
    POST response — never on subsequent GETs, since we don't store the raw."""
    id: UUID
    label: str
    created_at: str
    last_used_at: str | None
    token: str | None = None


def _label_from_email(email: str | None) -> str:
    """Derive a telemetry label from the user's email username. Falls back to
    'user' so the label is never empty. Sanitised to [a-z0-9._-] so it's safe
    in logs and dashboards."""
    local = (email or "").split("@", 1)[0].strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]", "", local)
    return cleaned or "user"


@router.get("/mcp/tokens")
async def list_mcp_tokens(user: AuthUser = Depends(require_user)) -> dict[str, list[dict]]:
    def _do() -> list[dict]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT id, label, created_at, last_used_at
                FROM mcp_tokens
                WHERE user_id = %s AND revoked_at IS NULL
                ORDER BY created_at DESC
                """,
                (str(user.user_id),),
            )
            return [
                {
                    "id": str(r[0]),
                    "label": r[1],
                    "created_at": r[2].isoformat(),
                    "last_used_at": r[3].isoformat() if r[3] else None,
                }
                for r in cur.fetchall()
            ]

    return {"tokens": await asyncio.to_thread(_do)}


@router.post("/mcp/tokens", response_model=McpTokenOut)
async def mint_mcp_token(user: AuthUser = Depends(require_user)) -> McpTokenOut:
    """Mint a new bearer token for this user. The raw value is returned exactly
    once — we only persist its sha256 hash. Subsequent GETs never include the
    raw token. Lost it? Mint a new one and revoke the old."""
    raw_token = "tok_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    label = _label_from_email(user.email)

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_tokens (user_id, token_hash, label)
                VALUES (%s, %s, %s)
                RETURNING id, label, created_at, last_used_at
                """,
                (str(user.user_id), token_hash, label),
            )
            row = cur.fetchone()
            c.commit()
            return {
                "id": str(row[0]),
                "label": row[1],
                "created_at": row[2].isoformat(),
                "last_used_at": row[3].isoformat() if row[3] else None,
            }

    result = await asyncio.to_thread(_do)
    return McpTokenOut(**result, token=raw_token)


@router.delete("/mcp/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_mcp_token(
    token_id: UUID,
    user: AuthUser = Depends(require_user),
) -> None:
    def _do() -> None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE mcp_tokens
                SET revoked_at = now()
                WHERE id = %s AND user_id = %s AND revoked_at IS NULL
                """,
                (str(token_id), str(user.user_id)),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="token not found")
            c.commit()

    await asyncio.to_thread(_do)


# --------------------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------------------


@router.post("/messages/{message_id}/feedback")
async def submit_feedback(
    message_id: UUID,
    body: FeedbackBody,
    user: AuthUser = Depends(require_user),
) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT m.query_id
                FROM messages m
                JOIN conversations conv ON conv.id = m.conversation_id
                WHERE m.id = %s AND conv.user_id = %s AND conv.deleted_at IS NULL
                  AND m.role = 'assistant'
                """,
                (str(message_id), str(user.user_id)),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="message not found")
            query_id = row[0]
            if query_id is None:
                raise HTTPException(status_code=409, detail="message has no query record yet")
            cur.execute(
                "UPDATE queries SET feedback = %s, feedback_text = %s WHERE id = %s",
                (body.rating, f"web:{user.user_id}:{body.text or ''}"[:500], query_id),
            )
            c.commit()
            return {"ok": True, "query_id": str(query_id), "rating": body.rating}

    return await asyncio.to_thread(_do)


# --------------------------------------------------------------------------------------
# Org view — role + folder-access management (admin only)
# --------------------------------------------------------------------------------------
#
# This is the single control surface for access. Changing a member's role or a
# folder's required level here takes effect across EVERY surface (web chat, Slack
# bot, MCP) because they all resolve clearance from these same tables at query
# time. Folder-rule edits re-stamp documents.min_level via recompute.


async def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    """FastAPI dependency: 403 unless the caller is an org admin."""
    if not await asyncio.to_thread(is_admin, user.email):
        raise HTTPException(status_code=403, detail="admin only")
    return user


class OrgMeOut(BaseModel):
    email: str | None
    level: int
    is_admin: bool


class RoleOut(BaseModel):
    id: int
    name: str
    level: int


class MemberOut(BaseModel):
    email: str
    role_id: int | None
    role_name: str | None
    level: int | None
    is_admin: bool
    last_sign_in_at: str | None


class MemberPatchBody(BaseModel):
    role_id: int | None = None  # null clears the role (→ level 0)
    is_admin: bool | None = None


class FolderRuleOut(BaseModel):
    id: UUID
    folder_prefix: list[str]
    min_level: int
    note: str | None


class FolderOut(BaseModel):
    folder_path: list[str]
    n_docs: int
    effective_min_level: int


class FolderAclBody(BaseModel):
    folder_prefix: list[str] = Field(min_length=1)
    min_level: int = Field(ge=1, le=4)
    note: str | None = Field(default=None, max_length=300)


@router.get("/org/me", response_model=OrgMeOut)
async def org_me(user: AuthUser = Depends(require_user)) -> OrgMeOut:
    """Any logged-in user: their own level + whether they may open the org view."""
    def _do() -> dict:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COALESCE((SELECT r.level FROM members m
                            LEFT JOIN roles r ON r.id = m.role_id
                            WHERE m.email = lower(%s)), 0),
                  COALESCE((SELECT is_admin FROM members WHERE email = lower(%s)), false)
                """,
                (user.email, user.email),
            )
            level, admin = cur.fetchone()
            return {"email": user.email, "level": int(level), "is_admin": bool(admin)}

    return OrgMeOut(**await asyncio.to_thread(_do))


class AccessNoteOut(BaseModel):
    name: str
    n_docs: int
    is_public: bool


class MyAccessOut(BaseModel):
    total_notes: int
    total_docs: int
    notes: list[AccessNoteOut]


@router.get("/org/me/access", response_model=MyAccessOut)
async def org_me_access(user: AuthUser = Depends(require_user)) -> MyAccessOut:
    """Any logged-in user: the NotePM notebooks they can access (derived from
    their NotePM permissions) with per-notebook document counts. Read-only —
    NotePM is the source of truth, there is nothing to edit here."""
    def _do() -> dict:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT n.name, n.is_public, count(d.id) AS n_docs
                FROM notepm_notes n
                JOIN documents d
                  ON d.note_code = n.note_code
                 AND d.source = 'notepm' AND d.deleted_at IS NULL
                WHERE n.is_public
                   OR n.note_code IN (
                        SELECT note_code FROM notepm_note_access
                        WHERE user_code = (SELECT notepm_user_code
                                           FROM members WHERE email = lower(%s))
                      )
                GROUP BY n.name, n.is_public
                ORDER BY n.name
                """,
                (user.email,),
            )
            rows = cur.fetchall()
        notes = [{"name": r[0], "is_public": bool(r[1]), "n_docs": int(r[2])} for r in rows]
        return {
            "total_notes": len(notes),
            "total_docs": sum(n["n_docs"] for n in notes),
            "notes": notes,
        }

    return MyAccessOut(**await asyncio.to_thread(_do))


@router.get("/org/roles")
async def org_roles(_: AuthUser = Depends(require_admin)) -> dict[str, list[dict]]:
    def _do() -> list[dict]:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, name, level FROM roles ORDER BY level")
            return [{"id": r[0], "name": r[1], "level": r[2]} for r in cur.fetchall()]

    return {"roles": await asyncio.to_thread(_do)}


@router.get("/org/members")
async def org_members(_: AuthUser = Depends(require_admin)) -> dict[str, list[dict]]:
    """Everyone who has logged into the web app OR has a member row (covers
    pre-provisioned / Slack-only people), with their assigned role + admin flag."""
    def _do() -> list[dict]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                WITH emails AS (
                    SELECT lower(email) AS email FROM auth.users WHERE email IS NOT NULL
                    UNION
                    SELECT email FROM members
                ),
                signin AS (
                    SELECT lower(email) AS email, max(last_sign_in_at) AS last_sign_in_at
                    FROM auth.users WHERE email IS NOT NULL GROUP BY lower(email)
                )
                SELECT e.email, m.role_id, r.name, r.level,
                       COALESCE(m.is_admin, false), s.last_sign_in_at
                FROM emails e
                LEFT JOIN members m ON m.email = e.email
                LEFT JOIN roles r ON r.id = m.role_id
                LEFT JOIN signin s ON s.email = e.email
                ORDER BY e.email
                """
            )
            return [
                {
                    "email": r[0],
                    "role_id": r[1],
                    "role_name": r[2],
                    "level": r[3],
                    "is_admin": r[4],
                    "last_sign_in_at": r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]

    return {"members": await asyncio.to_thread(_do)}


@router.put("/org/members/{email}", response_model=MemberOut)
async def update_member(
    email: str,
    body: MemberPatchBody,
    _: AuthUser = Depends(require_admin),
) -> MemberOut:
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    def _do() -> dict:
        with conn() as c, c.cursor() as cur:
            # Validate role_id if provided.
            if body.role_id is not None:
                cur.execute("SELECT 1 FROM roles WHERE id = %s", (body.role_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=400, detail="invalid role_id")

            cur.execute("SELECT role_id, is_admin FROM members WHERE email = %s", (email,))
            existing = cur.fetchone()
            cur_role = existing[0] if existing else None
            cur_admin = existing[1] if existing else False

            # Honour an explicit null (clear role) vs an omitted field.
            new_role = body.role_id if "role_id" in body.model_fields_set else cur_role
            new_admin = body.is_admin if body.is_admin is not None else cur_admin

            # Never let the org lock itself out: block removing the last admin.
            if cur_admin and not new_admin:
                cur.execute(
                    "SELECT count(*) FROM members WHERE is_admin AND email <> %s", (email,)
                )
                if cur.fetchone()[0] == 0:
                    raise HTTPException(status_code=400, detail="cannot remove the last admin")

            cur.execute(
                """
                INSERT INTO members (email, role_id, is_admin, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (email) DO UPDATE
                    SET role_id = EXCLUDED.role_id,
                        is_admin = EXCLUDED.is_admin,
                        updated_at = now()
                """,
                (email, new_role, new_admin),
            )
            cur.execute(
                """
                SELECT m.email, m.role_id, r.name, r.level, m.is_admin
                FROM members m LEFT JOIN roles r ON r.id = m.role_id
                WHERE m.email = %s
                """,
                (email,),
            )
            r = cur.fetchone()
            c.commit()
            return {
                "email": r[0], "role_id": r[1], "role_name": r[2],
                "level": r[3], "is_admin": r[4], "last_sign_in_at": None,
            }

    return MemberOut(**await asyncio.to_thread(_do))


@router.get("/org/folders")
async def org_folders(_: AuthUser = Depends(require_admin)) -> dict[str, list[dict]]:
    """The corpus folder tree (distinct folder_path with doc counts + current
    effective level) plus the explicit folder_acl rules driving them."""
    def _do() -> dict[str, list[dict]]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT folder_path, count(*), max(min_level)
                FROM documents
                WHERE deleted_at IS NULL AND cardinality(folder_path) >= 1
                GROUP BY folder_path
                ORDER BY folder_path
                """
            )
            folders = [
                {"folder_path": r[0], "n_docs": r[1], "effective_min_level": r[2]}
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT id, folder_prefix, min_level, note FROM folder_acl ORDER BY folder_prefix"
            )
            rules = [
                {"id": str(r[0]), "folder_prefix": r[1], "min_level": r[2], "note": r[3]}
                for r in cur.fetchall()
            ]
            return {"folders": folders, "rules": rules}

    return await asyncio.to_thread(_do)


@router.post("/org/folder-acl", response_model=FolderRuleOut)
async def upsert_folder_acl(
    body: FolderAclBody,
    user: AuthUser = Depends(require_admin),
) -> FolderRuleOut:
    """Set (or update) the minimum clearance level for a folder. Upserts by
    folder_prefix, then re-stamps documents.min_level so the change is live
    on every surface immediately."""
    def _do() -> dict:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO folder_acl (folder_prefix, min_level, note, created_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (folder_prefix) DO UPDATE
                    SET min_level = EXCLUDED.min_level,
                        note = EXCLUDED.note,
                        updated_at = now()
                RETURNING id, folder_prefix, min_level, note
                """,
                (body.folder_prefix, body.min_level, body.note, user.email),
            )
            r = cur.fetchone()
            c.commit()
            return {"id": r[0], "folder_prefix": r[1], "min_level": r[2], "note": r[3]}

    result = await asyncio.to_thread(_do)
    await asyncio.to_thread(recompute_document_levels)
    return FolderRuleOut(**result)


@router.delete("/org/folder-acl/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder_acl(rule_id: UUID, _: AuthUser = Depends(require_admin)) -> None:
    def _do() -> None:
        with conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM folder_acl WHERE id = %s", (str(rule_id),))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="rule not found")
            c.commit()

    await asyncio.to_thread(_do)
    await asyncio.to_thread(recompute_document_levels)


# --------------------------------------------------------------------------------------
# Meetings — the 商談AI surface. See docs/MEETINGS_WEB.md.
#
# Two callers, two kinds of auth. The AI participant runs on a VPS with no
# Supabase user, so its writes carry a service token in X-Meeting-Agent-Token
# (§6.1). The browser reads with the normal user JWT and is gated on the
# Calendar attendee list (§5). Every query filters on the caller's email
# explicitly; the RLS policies in 014_meetings.sql are defence-in-depth.
# --------------------------------------------------------------------------------------

_MEETING_STATUSES = {"scheduled", "joining", "live", "ended", "failed"}
_AGENT_STATES = {"asleep", "open"}
_ALLOWED_SHARE_DOMAIN = "@gastroduce-japan.co.jp"

# How much transcript to put in front of the model. A 60-minute meeting is very
# roughly 40k characters of captions; the tail is what people ask about, so both
# caps take the end. Raise if summaries start missing the opening.
_TRANSCRIPT_PROMPT_CHARS = 16_000
_SUMMARY_TRANSCRIPT_CHARS = 60_000


class MeetingAttendee(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    is_organizer: bool = False


class MeetingUpsertBody(BaseModel):
    google_event_id: str = Field(min_length=1, max_length=1024)
    title: str | None = Field(default=None, max_length=500)
    meet_url: str | None = Field(default=None, max_length=1000)
    scheduled_at: datetime
    # The access-control list (§5). Empty is accepted but leaves the meeting
    # readable by nobody, so it is logged as a warning rather than silently kept.
    attendees: list[MeetingAttendee] = Field(default_factory=list)


class MeetingPatchBody(BaseModel):
    """Both callers PATCH this path (§6.2). The agent may set status,
    started_at and agent_state; the browser may set title. Each is rejected on
    the other's fields."""

    title: str | None = Field(default=None, max_length=500)
    status: str | None = None
    started_at: datetime | None = None
    # Agent-only. The 90 s idle expiry out of `open` is timed on the meeting
    # side (§6.4), so the agent needs a way to write the result back. Without
    # it the row stays `open` after the gate has closed, the agent re-reads its
    # own stale value on the next 3 s poll and re-opens, and the timeout never
    # takes effect — while the UI shows a state the agent is not in.
    agent_state: str | None = None


class SegmentIn(BaseModel):
    seq: int = Field(ge=0)
    speaker: str = Field(max_length=200)
    text: str = Field(max_length=10_000)
    spoken_at: datetime


class SegmentsBody(BaseModel):
    segments: list[SegmentIn] = Field(default_factory=list, max_length=500)


class MeetingEndBody(BaseModel):
    ended_at: datetime | None = None


class AgentStateBody(BaseModel):
    agent_state: str


class MeetingShareBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)


def meeting_visible_sql(alias: str = "m") -> str:
    """SQL predicate for "this caller may read this meeting". Takes one %s
    parameter: the caller's email.

    Kept as a function, like `retrieve.access_sql`, so the gate is written once
    and can be unit-tested without a database."""
    return (
        f"{alias}.deleted_at IS NULL AND EXISTS ("
        "SELECT 1 FROM meeting_participants p "
        f"WHERE p.meeting_id = {alias}.id AND p.email = lower(%s))"
    )


async def require_meeting_agent(
    x_meeting_agent_token: str | None = Header(default=None),
) -> None:
    """The VPS's service token (§6.1). Constant-time compare; an unset env var
    closes the write paths rather than opening them."""
    expected = get_settings().meeting_agent_token
    if not expected:
        raise HTTPException(status_code=503, detail="meeting agent token not configured")
    if not x_meeting_agent_token or not hmac.compare_digest(x_meeting_agent_token, expected):
        raise HTTPException(status_code=401, detail="invalid meeting agent token")


async def meeting_agent_or_user(
    authorization: str | None = Header(default=None),
    x_meeting_agent_token: str | None = Header(default=None),
) -> AuthUser | None:
    """Dual auth for the one path both callers share. Returns None for the
    meeting agent, an AuthUser for a person."""
    expected = get_settings().meeting_agent_token
    if (
        x_meeting_agent_token
        and expected
        and hmac.compare_digest(x_meeting_agent_token, expected)
    ):
        return None
    return await require_user(authorization)


def _require_participant(cur, meeting_id: UUID, email: str | None) -> None:
    """404 (not 403) unless the caller is on the meeting's participant list.
    404 so the endpoint cannot be used to probe which meetings exist."""
    cur.execute(
        f"SELECT 1 FROM meetings m WHERE m.id = %s AND {meeting_visible_sql()}",
        (str(meeting_id), email or ""),
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="meeting not found")


def _row_to_meeting(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "title": row[1],
        "meet_url": row[2],
        "scheduled_at": row[3].isoformat(),
        "started_at": row[4].isoformat() if row[4] else None,
        "ended_at": row[5].isoformat() if row[5] else None,
        "status": row[6],
        "agent_state": row[7],
        "summary_status": row[8],
        "participant_count": row[9],
    }


_MEETING_COLUMNS = """
    m.id, m.title, m.meet_url, m.scheduled_at, m.started_at, m.ended_at,
    m.status, m.agent_state, m.summary_status,
    (SELECT count(*) FROM meeting_participants mp WHERE mp.meeting_id = m.id)
"""


# --------------------------------------------------------------------------------------
# Meetings — the agent-facing write paths (§6.2)
# --------------------------------------------------------------------------------------


@router.post("/meetings", status_code=status.HTTP_200_OK)
async def upsert_meeting(
    body: MeetingUpsertBody,
    _: None = Depends(require_meeting_agent),
) -> dict[str, Any]:
    """Called by the calendar watcher when it discovers the invite, and again by
    the participant as it joins. Idempotent on google_event_id."""

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO meetings (google_event_id, title, meet_url, scheduled_at)
                VALUES (%s, COALESCE(%s, '(無題の会議)'), %s, %s)
                ON CONFLICT (google_event_id) DO UPDATE SET
                    -- A human rename wins over the calendar's title from then on.
                    title = CASE WHEN meetings.renamed_at IS NULL
                                 THEN COALESCE(EXCLUDED.title, meetings.title)
                                 ELSE meetings.title END,
                    meet_url = COALESCE(EXCLUDED.meet_url, meetings.meet_url),
                    scheduled_at = EXCLUDED.scheduled_at
                RETURNING id, status, agent_state
                """,
                (body.google_event_id, body.title, body.meet_url, body.scheduled_at),
            )
            meeting_id, mstatus, agent_state = cur.fetchone()

            if body.attendees:
                # Calendar is the source of truth for its own rows; rows a person
                # added through "share with…" (added_by set) survive the re-sync.
                cur.execute(
                    "DELETE FROM meeting_participants "
                    "WHERE meeting_id = %s AND added_by IS NULL",
                    (str(meeting_id),),
                )
                for a in body.attendees:
                    cur.execute(
                        """
                        INSERT INTO meeting_participants (meeting_id, email, is_organizer)
                        VALUES (%s, lower(%s), %s)
                        ON CONFLICT (meeting_id, email) DO NOTHING
                        """,
                        (str(meeting_id), a.email.strip(), a.is_organizer),
                    )
            else:
                log.warning(
                    "meetings: upsert with no attendees event=%s — nobody can read it",
                    body.google_event_id,
                )
            c.commit()
            return {
                "id": str(meeting_id),
                "status": mstatus,
                "agent_state": agent_state,
            }

    return await asyncio.to_thread(_do)


@router.patch("/meetings/{meeting_id}")
async def patch_meeting(
    meeting_id: UUID,
    body: MeetingPatchBody,
    caller: AuthUser | None = Depends(meeting_agent_or_user),
) -> dict[str, Any]:
    """Agent: status, started_at, agent_state (the 90 s expiry writing itself
    back, §6.4). Browser: title (§7.1's editable title)."""
    is_agent = caller is None

    sets: list[str] = []
    params: list[Any] = []
    if is_agent:
        if body.title is not None:
            raise HTTPException(status_code=403, detail="the agent may not rename a meeting")
        if body.status is not None:
            if body.status not in _MEETING_STATUSES:
                raise HTTPException(status_code=400, detail="invalid status")
            sets.append("status = %s")
            params.append(body.status)
        if body.started_at is not None:
            sets.append("started_at = %s")
            params.append(body.started_at)
        if body.agent_state is not None:
            if body.agent_state not in _AGENT_STATES:
                raise HTTPException(status_code=400, detail="invalid agent_state")
            sets.append("agent_state = %s")
            params.append(body.agent_state)
    else:
        if body.status is not None or body.started_at is not None:
            raise HTTPException(status_code=403, detail="only the agent may set status")
        if body.agent_state is not None:
            # Not a capability difference — the browser has POST /state for
            # this. One path per caller keeps "who last wrote it" readable.
            raise HTTPException(
                status_code=403, detail="use POST /v1/meetings/{id}/state"
            )
        if body.title is not None:
            sets.append("title = %s")
            sets.append("renamed_at = now()")
            params.append(body.title.strip() or "(無題の会議)")
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            if not is_agent:
                _require_participant(cur, meeting_id, caller.email)
            cur.execute(
                f"""
                UPDATE meetings m SET {', '.join(sets)}
                WHERE m.id = %s AND m.deleted_at IS NULL
                RETURNING {_MEETING_COLUMNS}
                """,
                [*params, str(meeting_id)],
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="meeting not found")
            c.commit()
            return _row_to_meeting(row)

    return await asyncio.to_thread(_do)


@router.post("/meetings/{meeting_id}/segments")
async def post_segments(
    meeting_id: UUID,
    body: SegmentsBody,
    _: None = Depends(require_meeting_agent),
) -> dict[str, int]:
    """Caption lines, batched every few seconds.

    The VPS retries on network failure and may resend a whole batch, so this is
    idempotent on (meeting_id, seq) and makes no assumption about ordering or
    exactly-once delivery (§6.2)."""
    if not body.segments:
        return {"inserted": 0}

    def _do() -> dict[str, int]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM meetings WHERE id = %s AND deleted_at IS NULL",
                (str(meeting_id),),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="meeting not found")

            values: list[Any] = []
            rows: list[str] = []
            for s in body.segments:
                rows.append("(%s, %s, %s, %s, %s)")
                values.extend([str(meeting_id), s.seq, s.speaker, s.text, s.spoken_at])
            cur.execute(
                f"""
                INSERT INTO meeting_segments (meeting_id, seq, speaker, text, spoken_at)
                VALUES {', '.join(rows)}
                ON CONFLICT (meeting_id, seq) DO NOTHING
                """,
                values,
            )
            inserted = cur.rowcount
            c.commit()
            return {"inserted": inserted}

    return await asyncio.to_thread(_do)


@router.post("/meetings/{meeting_id}/end", status_code=status.HTTP_202_ACCEPTED)
async def end_meeting(
    meeting_id: UUID,
    body: MeetingEndBody,
    background: BackgroundTasks,
    _: None = Depends(require_meeting_agent),
) -> dict[str, str]:
    """The AI is leaving. Summary generation runs after the response: the VPS is
    tearing the session down and should not hold a connection open for a model
    call. If the process dies before it finishes, summary_status stays 'pending'
    and the detail page's 再生成 button re-runs it."""

    def _do() -> None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE meetings
                SET status = 'ended',
                    ended_at = COALESCE(%s, now()),
                    summary_status = 'pending',
                    agent_state = 'asleep'
                WHERE id = %s AND deleted_at IS NULL
                """,
                (body.ended_at, str(meeting_id)),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="meeting not found")
            c.commit()

    await asyncio.to_thread(_do)
    background.add_task(_summarize_meeting_safe, meeting_id)
    return {"status": "ended"}


@router.get("/meetings/{meeting_id}/state")
async def get_meeting_state(
    meeting_id: UUID,
    _: None = Depends(require_meeting_agent),
) -> dict[str, str]:
    """Polled by the VPS every ~3 s. This is the only control channel — we never
    call the meeting agent (§6.3)."""

    def _do() -> dict[str, str]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT agent_state FROM meetings WHERE id = %s AND deleted_at IS NULL",
                (str(meeting_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="meeting not found")
            return {"agent_state": row[0]}

    return await asyncio.to_thread(_do)


# --------------------------------------------------------------------------------------
# Meetings — the browser-facing paths (§7.2)
# --------------------------------------------------------------------------------------


@router.get("/meetings")
async def list_meetings(
    limit: int = 50,
    user: AuthUser = Depends(require_user),
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_MEETING_COLUMNS}
                FROM meetings m
                WHERE {meeting_visible_sql()}
                ORDER BY m.scheduled_at DESC
                LIMIT %s
                """,
                (user.email or "", limit),
            )
            return {"meetings": [_row_to_meeting(r) for r in cur.fetchall()]}

    return await asyncio.to_thread(_do)


@router.get("/meetings/{meeting_id}")
async def get_meeting(
    meeting_id: UUID,
    user: AuthUser = Depends(require_user),
) -> dict[str, Any]:
    """Detail: the meeting, its transcript, its participants, its summary, and
    this user's own Q&A threads about it."""

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_MEETING_COLUMNS}, m.summary, m.next_actions
                FROM meetings m
                WHERE m.id = %s AND {meeting_visible_sql()}
                """,
                (str(meeting_id), user.email or ""),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="meeting not found")
            meeting = _row_to_meeting(row)
            meeting["summary"] = row[10]
            meeting["next_actions"] = row[11]

            cur.execute(
                """
                SELECT email, is_organizer, added_by IS NOT NULL
                FROM meeting_participants
                WHERE meeting_id = %s
                ORDER BY is_organizer DESC, email
                """,
                (str(meeting_id),),
            )
            participants = [
                {"email": r[0], "is_organizer": r[1], "shared": r[2]}
                for r in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT seq, speaker, text, spoken_at
                FROM meeting_segments
                WHERE meeting_id = %s
                ORDER BY seq
                """,
                (str(meeting_id),),
            )
            segments = [
                {
                    "seq": r[0],
                    "speaker": r[1],
                    "text": r[2],
                    "spoken_at": r[3].isoformat(),
                }
                for r in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT id, title, created_at, updated_at, archived_at
                FROM conversations
                WHERE meeting_id = %s AND user_id = %s AND deleted_at IS NULL
                ORDER BY updated_at DESC
                """,
                (str(meeting_id), str(user.user_id)),
            )
            threads = [_row_to_thread(r) for r in cur.fetchall()]

            return {
                "meeting": meeting,
                "participants": participants,
                "segments": segments,
                "threads": threads,
            }

    return await asyncio.to_thread(_do)


@router.post("/meetings/{meeting_id}/state")
async def set_meeting_state(
    meeting_id: UUID,
    body: AgentStateBody,
    user: AuthUser = Depends(require_user),
) -> dict[str, str]:
    """The asleep/open toggle on the live row. The VPS picks this up within ~3 s
    on its next poll; the 90 s idle expiry belongs to the meeting side, not here
    (§6.4)."""
    if body.agent_state not in _AGENT_STATES:
        raise HTTPException(status_code=400, detail="invalid agent_state")

    def _do() -> dict[str, str]:
        with conn() as c, c.cursor() as cur:
            _require_participant(cur, meeting_id, user.email)
            cur.execute(
                "UPDATE meetings SET agent_state = %s WHERE id = %s RETURNING agent_state",
                (body.agent_state, str(meeting_id)),
            )
            row = cur.fetchone()
            c.commit()
            return {"agent_state": row[0]}

    return await asyncio.to_thread(_do)


@router.post("/meetings/{meeting_id}/share")
async def share_meeting(
    meeting_id: UUID,
    body: MeetingShareBody,
    user: AuthUser = Depends(require_user),
) -> dict[str, Any]:
    """Grant a colleague who was not on the invite. Same-domain only: the login
    itself is domain-gated, so an outside address could never read the row and
    would just be a confusing no-op."""
    email = body.email.strip().lower()
    if not email.endswith(_ALLOWED_SHARE_DOMAIN):
        raise HTTPException(status_code=400, detail="社内アドレスのみ共有できます")

    def _do() -> dict[str, Any]:
        with conn() as c, c.cursor() as cur:
            _require_participant(cur, meeting_id, user.email)
            cur.execute(
                """
                INSERT INTO meeting_participants (meeting_id, email, added_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (meeting_id, email) DO NOTHING
                """,
                (str(meeting_id), email, (user.email or "").lower()),
            )
            c.commit()
            return {"email": email}

    return await asyncio.to_thread(_do)


@router.delete("/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(meeting_id: UUID, user: AuthUser = Depends(require_user)) -> None:
    def _do() -> None:
        with conn() as c, c.cursor() as cur:
            _require_participant(cur, meeting_id, user.email)
            cur.execute(
                "UPDATE meetings SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL",
                (str(meeting_id),),
            )
            c.commit()

    await asyncio.to_thread(_do)


@router.post("/meetings/{meeting_id}/summary", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_summary(
    meeting_id: UUID,
    background: BackgroundTasks,
    user: AuthUser = Depends(require_user),
) -> dict[str, str]:
    """Re-run the summary. Flownote has the same button; here it doubles as the
    recovery path when the background task on /end did not survive."""

    def _do() -> None:
        with conn() as c, c.cursor() as cur:
            _require_participant(cur, meeting_id, user.email)
            cur.execute(
                "UPDATE meetings SET summary_status = 'pending' WHERE id = %s",
                (str(meeting_id),),
            )
            c.commit()

    await asyncio.to_thread(_do)
    background.add_task(_summarize_meeting_safe, meeting_id)
    return {"summary_status": "pending"}


# --------------------------------------------------------------------------------------
# Meeting summary generation (§7.4)
# --------------------------------------------------------------------------------------


_MEETING_SUMMARY_SYSTEM = """あなたは社内会議の議事録作成者です。
文字起こしを読み、日本語で要約とネクストアクションをまとめます。

ルール:
1. 出力は次の形式のJSONのみ。前置き・コードフェンス・説明を付けない。
   {"summary": "…", "next_actions": [{"text": "…", "owner": "…"}]}
2. summary は Markdown。「## 決定事項」「## 論点」「## 共有事項」のうち
   該当する見出しのみを使い、各項目は箇条書き1〜2文でまとめる。
3. next_actions は会議中に決まった具体的な行動のみ。owner は発言者名、
   担当が決まっていなければ空文字にする。行動が無ければ空配列。
4. 文字起こしに無いことを推測して書かない。聞き取れていない箇所は無視する。
5. Slackにそのまま投稿できる簡潔さを保つ。"""


def _transcript_text(cur, meeting_id: UUID, max_chars: int) -> str:
    """Speaker-labelled transcript, tail-truncated to max_chars."""
    cur.execute(
        """
        SELECT speaker, text FROM meeting_segments
        WHERE meeting_id = %s ORDER BY seq
        """,
        (str(meeting_id),),
    )
    lines = [f"{r[0]}: {r[1]}" for r in cur.fetchall()]
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = "（前半省略）\n" + body[-max_chars:]
    return body


def _summarize_meeting_safe(meeting_id: UUID) -> None:
    """Background entry point — never raises into the request that scheduled it."""
    try:
        _summarize_meeting(meeting_id)
    except Exception:
        log.exception("meeting summary failed meeting=%s", meeting_id)
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute(
                    "UPDATE meetings SET summary_status = 'failed' WHERE id = %s",
                    (str(meeting_id),),
                )
                c.commit()
        except Exception:
            log.exception("could not mark summary failed meeting=%s", meeting_id)


def _summarize_meeting(meeting_id: UUID) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT title FROM meetings WHERE id = %s AND deleted_at IS NULL",
            (str(meeting_id),),
        )
        row = cur.fetchone()
        if not row:
            return
        title = row[0]
        transcript = _transcript_text(cur, meeting_id, _SUMMARY_TRANSCRIPT_CHARS)

    if not transcript.strip():
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE meetings
                SET summary = %s, next_actions = '[]'::jsonb, summary_status = 'ready'
                WHERE id = %s
                """,
                ("文字起こしが取得できなかったため、要約はありません。", str(meeting_id)),
            )
            c.commit()
        return

    resp = llm.complete(
        system=_MEETING_SUMMARY_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"会議名: {title}\n\n--- 文字起こし ---\n{transcript}",
            }
        ],
        max_tokens=1500,
    )
    summary, next_actions = _parse_summary(resp.text)

    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE meetings
            SET summary = %s, next_actions = %s::jsonb, summary_status = 'ready'
            WHERE id = %s
            """,
            (summary, json.dumps(next_actions, ensure_ascii=False), str(meeting_id)),
        )
        c.commit()
    log.info("meeting summary written meeting=%s actions=%d", meeting_id, len(next_actions))


def _parse_summary(raw: str) -> tuple[str, list[dict]]:
    """Pull summary + next_actions out of the model's reply.

    The prompt asks for bare JSON, but a model that wraps it in a fence or adds a
    sentence must not cost us the summary — fall back to storing the text as-is
    rather than failing the whole job."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
        summary = str(data.get("summary", "")).strip()
        actions = data.get("next_actions") or []
        if not isinstance(actions, list):
            actions = []
        cleaned = [
            {"text": str(a.get("text", "")).strip(), "owner": str(a.get("owner", "")).strip()}
            for a in actions
            if isinstance(a, dict) and str(a.get("text", "")).strip()
        ]
        if summary:
            return summary, cleaned
    except (ValueError, AttributeError):
        log.warning("meeting summary was not JSON; storing raw text")
    return raw.strip(), []


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _sse(event: str, data: dict) -> dict:
    """sse-starlette consumes dicts with 'event' and 'data' keys."""
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


def _row_to_thread(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "title": row[1],
        "created_at": row[2].isoformat(),
        "updated_at": row[3].isoformat(),
        "archived_at": row[4].isoformat() if row[4] else None,
    }


def _shape_citations_from_chunks(chunks: list[RetrievedChunk]) -> list[dict]:
    """Build the citation payload sent to the web client during streaming.

    Numbers match the `[N]` markers the LLM is told to emit (assign_source_numbers
    groups by document so a single `[1]` can correspond to multiple chunks of the
    same NotePM page). The client renders these as hoverable chips."""
    if not chunks:
        return []
    nums = assign_source_numbers(chunks)
    by_n: dict[int, dict] = {}
    for n, c in zip(nums, chunks):
        entry = by_n.setdefault(
            n,
            {
                "n": n,
                "doc_title": c.doc_title,
                "doc_url": c.doc_url,
                "heading_path": [],
                "snippet": c.content[:240],
            },
        )
        heading = " › ".join(h for h in c.heading_path if h) if c.heading_path else ""
        if heading and heading not in entry["heading_path"]:
            entry["heading_path"].append(heading)
    return [by_n[n] for n in sorted(by_n)]


def _shape_citations(snapshot: list[dict]) -> list[dict]:
    """Build the citation payload from a stored snapshot (no rerank scores)."""
    if not snapshot:
        return []
    out: list[dict] = []
    seen: dict[str, dict] = {}
    n = 0
    for s in snapshot:
        key = s.get("doc_url") or s.get("doc_title") or s["chunk_id"]
        if key in seen:
            entry = seen[key]
            heading = " › ".join(s.get("heading_path") or [])
            if heading and heading not in entry["heading_path"]:
                entry["heading_path"].append(heading)
            continue
        n += 1
        entry = {
            "n": n,
            "doc_title": s.get("doc_title"),
            "doc_url": s.get("doc_url"),
            "heading_path": [" › ".join(s["heading_path"])] if s.get("heading_path") else [],
            "snippet": s.get("snippet", ""),
        }
        seen[key] = entry
        out.append(entry)
    return out


_VOCAB_TTL_S = 6 * 3600
_VOCAB_MAX_TERMS = 120
# Cached per access-scope, keyed by (user_code, slack_user_id) — two users with
# the same visibility share an entry. TTL matches the sales catalog's.
_vocab_cache: dict[tuple, tuple[float, list[str]]] = {}


def _voice_vocab_terms(email: str | None) -> list[str]:
    """Proper nouns worth biasing the speech transcriber toward: EC store names,
    mall names, and the titles of documents this user can actually see.

    Best-effort throughout — a missing BigQuery catalog or a slow DB must never
    stop a voice session from starting, so every failure degrades to fewer terms."""
    scope = resolve_access(email)
    key = (scope.user_code, scope.slack_user_id, scope.see_all)
    hit = _vocab_cache.get(key)
    if hit and hit[0] > time.time() - _VOCAB_TTL_S:
        return hit[1]

    terms: list[str] = []
    settings = get_settings()
    if settings.sales_bq_enabled:
        try:
            from gastrobrain.sales_bq import sales_schema

            catalogs = sales_schema().get("catalogs") or {}
            terms += [str(s) for s in catalogs.get("store_ids", [])]
            terms += [str(p) for p in catalogs.get("ec_platforms", [])]
        except Exception:
            log.warning("voice vocab: sales catalogs unavailable", exc_info=True)

    try:
        access_clause, params = access_sql(scope)
        with conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                SELECT d.title
                FROM documents d
                WHERE d.deleted_at IS NULL
                  AND d.source = 'notepm'
                  {access_clause}
                ORDER BY d.updated_at DESC NULLS LAST
                LIMIT %s
                """,
                [*params, _VOCAB_MAX_TERMS],
            )
            terms += [r[0] for r in cur.fetchall() if r[0]]
    except Exception:
        log.warning("voice vocab: document titles unavailable", exc_info=True)

    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        t = t.strip()
        # Long titles are sentences, not nouns — they dilute the decoding hint.
        if not t or len(t) > 40 or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= _VOCAB_MAX_TERMS:
            break

    _vocab_cache[key] = (time.time(), out)
    return out


def _prep_turn(
    *,
    conversation_id: UUID,
    user: AuthUser,
    question: str,
    stored_question: str | None = None,
) -> tuple[list[HistoryTurn], UUID, UserPreferences | None, AccessScope, str | None]:
    """Verify thread ownership, resolve access scope, load the history window
    and prefs, and insert the user's turn — all in one transaction.

    The fifth return value is the meeting record when the thread belongs to
    a meeting, and None otherwise. Participation is re-checked here rather than
    only at thread creation: access can be revoked between the two.

    Shared by the streaming `/chat` and the non-streaming `/voice/ask` so both
    surfaces are gated by exactly the same ACL and see the same history.

    `stored_question` overrides what lands in `messages` while `question` still
    drives retrieval. Voice needs this split: the voice agent sends a
    context-expanded query, but the thread has to read back as what the person
    actually said, or 「チャットで続ける」 shows a transcript nobody recognises."""
    history_window = get_settings().web_history_window
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT meeting_id FROM conversations
            WHERE id = %s AND user_id = %s AND deleted_at IS NULL
            """,
            (str(conversation_id), str(user.user_id)),
        )
        conv_row = cur.fetchone()
        if not conv_row:
            raise HTTPException(status_code=404, detail="thread not found")

        meeting_context: str | None = None
        if conv_row[0] is not None:
            _require_participant(cur, conv_row[0], user.email)
            cur.execute(
                "SELECT title, summary, scheduled_at, started_at, ended_at, status "
                "FROM meetings WHERE id = %s",
                (str(conv_row[0]),),
            )
            m_title, m_summary, scheduled_at, started_at, ended_at, m_status = cur.fetchone()
            cur.execute(
                "SELECT email, is_organizer FROM meeting_participants "
                "WHERE meeting_id = %s AND added_by IS NULL "
                "ORDER BY is_organizer DESC, email",
                (str(conv_row[0]),),
            )
            invitees = cur.fetchall()
            # Read speakers from ALL segments, not just the retained transcript
            # tail: an early speaker must not disappear from attendance answers.
            cur.execute(
                "SELECT DISTINCT speaker FROM meeting_segments "
                "WHERE meeting_id = %s AND btrim(speaker) <> '' ORDER BY speaker",
                (str(conv_row[0]),),
            )
            speakers = [r[0] for r in cur.fetchall()]
            transcript = _transcript_text(cur, conv_row[0], _TRANSCRIPT_PROMPT_CHARS)
            meeting_context = format_meeting_record(
                meeting_id=str(conv_row[0]), title=m_title, summary=m_summary,
                scheduled_at=scheduled_at, started_at=started_at, ended_at=ended_at,
                status=m_status, invitees=invitees, speakers=speakers, transcript=transcript,
            )

        # Access scope (gates which docs retrieval may surface). Resolved by
        # email — the universal identity across web/Slack/MCP. No member row
        # or no NotePM account → public-only (fail-closed).
        cur.execute(
            "SELECT notepm_user_code, slack_user_id FROM members WHERE email = lower(%s)",
            (user.email,),
        )
        row = cur.fetchone()
        scope = AccessScope(user_code=row[0], slack_user_id=row[1]) if row else PUBLIC_ONLY

        cur.execute(
            """
            SELECT role, content FROM messages
            WHERE conversation_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (str(conversation_id), history_window),
        )
        rows = list(reversed(cur.fetchall()))
        history: list[HistoryTurn] = [{"role": r[0], "content": r[1]} for r in rows]

        cur.execute(
            """
            INSERT INTO messages (conversation_id, role, content)
            VALUES (%s, 'user', %s)
            RETURNING id
            """,
            (str(conversation_id), stored_question or question),
        )
        user_msg_id = cur.fetchone()[0]

        cur.execute(
            "SELECT department, extra_note FROM user_preferences WHERE user_id = %s",
            (str(user.user_id),),
        )
        prefs_row = cur.fetchone()
        if prefs_row and (prefs_row[0] or prefs_row[1]):
            prefs = UserPreferences(department=prefs_row[0], extra_note=prefs_row[1])
        else:
            prefs = None

        c.commit()
        return history, user_msg_id, prefs, scope, meeting_context


def _persist_assistant_turn(
    *,
    conversation_id: UUID,
    user_id: UUID,
    question: str,
    final: AnswerDone,
) -> tuple[UUID, UUID | None]:
    """Insert the queries row + the assistant messages row in a single transaction."""
    cited_ids = [c.chunk_id for c in final.chunks]
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO queries
              (user_id, question, answer, cited_chunks, retrieved_chunks,
               latency_ms, input_tokens, output_tokens, cost_jpy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(user_id),
                question,
                final.answer,
                cited_ids,
                cited_ids,
                final.latency_ms,
                final.input_tokens,
                final.output_tokens,
                final.cost_jpy,
            ),
        )
        query_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO messages (conversation_id, role, content, cited_chunks, query_id)
            VALUES (%s, 'assistant', %s, %s, %s)
            RETURNING id
            """,
            (str(conversation_id), final.answer, cited_ids, query_id),
        )
        message_id = cur.fetchone()[0]
        c.commit()
        return message_id, query_id
