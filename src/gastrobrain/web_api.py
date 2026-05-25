"""Web API router — Next.js (and any other future surface) talks to this.

Mounted onto the same FastAPI app as the Slack handler. Endpoints under /v1/*.
Auth: Supabase HS256 JWT via the `require_user` dependency. Ownership is
enforced at the SQL layer (every query filters on user_id = $1); the RLS
policies in 002_web_chat.sql are defense-in-depth for direct DB access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from gastrobrain.auth import AuthUser, require_user
from gastrobrain.config import get_settings
from gastrobrain.db import conn
from gastrobrain.generate import HistoryTurn, UserPreferences

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
from gastrobrain.retrieve import RetrievedChunk
from gastrobrain.slack_format import assign_source_numbers

log = logging.getLogger("gastrobrain.web_api")

router = APIRouter(prefix="/v1")


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class ThreadCreateBody(BaseModel):
    title: str | None = None


class ThreadPatchBody(BaseModel):
    title: str | None = None
    archived: bool | None = None


class ChatBody(BaseModel):
    conversation_id: UUID
    question: str = Field(min_length=1, max_length=4000)


class FeedbackBody(BaseModel):
    rating: int = Field(ge=-1, le=1)
    text: str | None = Field(default=None, max_length=2000)


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
            cur.execute(
                """
                INSERT INTO conversations (user_id, title)
                VALUES (%s, COALESCE(%s, '新規チャット'))
                RETURNING id, title, created_at, updated_at, archived_at
                """,
                (str(user.user_id), body.title),
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

    s = get_settings()
    user_msg = (
        f"質問: {question.strip()[:400]}\n"
        f"回答(抜粋): {answer_text.strip()[:400]}\n\n"
        "上記の会話のタイトルを生成してください。"
    )
    try:
        resp = await asyncio.to_thread(
            lambda: anthropic.Anthropic(api_key=s.claude_api_key).messages.create(
                model=s.anthropic_haiku_model,
                max_tokens=64,
                system=[{"type": "text", "text": _TITLE_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_msg}],
            )
        )
        title = "".join(b.text for b in resp.content if b.type == "text").strip()
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
    settings = get_settings()
    history_window = settings.web_history_window

    # Verify ownership + load history + load prefs before opening the stream.
    # Failing fast here surfaces 4xx instead of a half-rendered SSE.
    def _prep() -> tuple[list[HistoryTurn], UUID, UserPreferences | None]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM conversations
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (str(body.conversation_id), str(user.user_id)),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="thread not found")

            cur.execute(
                """
                SELECT role, content FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(body.conversation_id), history_window),
            )
            rows = list(reversed(cur.fetchall()))
            history: list[HistoryTurn] = [{"role": r[0], "content": r[1]} for r in rows]

            cur.execute(
                """
                INSERT INTO messages (conversation_id, role, content)
                VALUES (%s, 'user', %s)
                RETURNING id
                """,
                (str(body.conversation_id), body.question),
            )
            user_msg_id = cur.fetchone()[0]

            cur.execute(
                "SELECT department, extra_note FROM user_preferences WHERE user_id = %s",
                (str(user.user_id),),
            )
            prefs_row = cur.fetchone()
            if prefs_row and (prefs_row[0] or prefs_row[1]):
                prefs = UserPreferences(
                    department=prefs_row[0],
                    extra_note=prefs_row[1],
                )
            else:
                prefs = None

            c.commit()
            return history, user_msg_id, prefs

    log.info("chat: prep start conversation=%s user=%s", body.conversation_id, user.user_id)
    history, _, prefs = await asyncio.to_thread(_prep)
    log.info("chat: prep done history_len=%d prefs=%s", len(history), prefs)

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
