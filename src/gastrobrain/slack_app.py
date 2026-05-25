from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
import traceback
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slack_sdk.web.async_client import AsyncWebClient

from gastrobrain.config import get_settings
from gastrobrain.db import conn
from gastrobrain.pipeline import run_pipeline_for_slack
from gastrobrain.retrieve import RetrievalStats, RetrievedChunk
from gastrobrain.slack_format import assign_source_numbers, split_to_section_blocks, to_slack_mrkdwn
from gastrobrain.web_api import router as web_router

log = logging.getLogger("gastrobrain.slack")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# --------------------------------------------------------------------------------------
# /mcp — Streamable HTTP MCP endpoint for external agents (Claude Code, etc.)
# --------------------------------------------------------------------------------------
#
# Built lazily so disabling the surface (env: GASTROBRAIN_MCP_ENABLED=false)
# skips importing the `mcp` package entirely. The session manager has to run
# inside the FastAPI lifespan or the streamable transport's task group
# won't start.

_mcp_enabled = get_settings().gastrobrain_mcp_enabled and bool(
    get_settings().gastrobrain_mcp_tokens.strip()
)


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    if not _mcp_enabled:
        log.info("MCP surface disabled (no tokens configured)")
        yield
        return
    from gastrobrain.mcp_server import mcp_lifespan_cm

    log.info("MCP surface enabled; entering session manager")
    async with mcp_lifespan_cm():
        yield


app = FastAPI(lifespan=_lifespan)
app.include_router(web_router)

if _mcp_enabled:
    from gastrobrain.mcp_server import build_mcp_asgi_app

    app.mount("/mcp", build_mcp_asgi_app())


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the traceback to Cloud Run logs and surface the exception class +
    message in the response body. Safe because this is an internal-only API
    (Slack-workspace-gated)."""
    log.exception("unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}",
            "path": request.url.path,
            "traceback": traceback.format_exc().splitlines()[-8:],
        },
    )


def _settings():
    s = get_settings()
    s.require("slack_bot_token", "slack_signing_secret", "claude_api_key", "cohere_api", "database_url")
    return s


_slack_client: AsyncWebClient | None = None


def _slack() -> AsyncWebClient:
    global _slack_client
    if _slack_client is None:
        _slack_client = AsyncWebClient(token=_settings().slack_bot_token)
    return _slack_client


@app.get("/up")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request, bg: BackgroundTasks) -> dict[str, Any]:
    body = await request.body()
    _verify_slack(request, body)
    payload = json.loads(body)

    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    event = payload.get("event", {})
    etype = event.get("type")

    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return {"ok": True}

    if etype == "app_mention":
        question = _strip_mention(event.get("text", ""))
        bg.add_task(
            _handle_event_question,
            question=question,
            channel=event["channel"],
            thread_ts=event.get("thread_ts") or event["ts"],
            user_id=event.get("user", "unknown"),
        )
    elif etype == "message" and event.get("channel_type") == "im":
        bg.add_task(
            _handle_event_question,
            question=event.get("text", ""),
            channel=event["channel"],
            thread_ts=None,
            user_id=event.get("user", "unknown"),
        )

    return {"ok": True}


@app.post("/slack/commands")
async def slack_commands(request: Request, bg: BackgroundTasks) -> dict[str, Any]:
    body = await request.body()
    _verify_slack(request, body)
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    question = form.get("text", "").strip()
    response_url = form["response_url"]
    user_id = form.get("user_id", "unknown")
    channel_id = form.get("channel_id", "")

    if not question:
        return {
            "response_type": "ephemeral",
            "text": "質問を入力してください。例: `/gastrobrain TTSのデイリーチェックリストは？`",
        }

    bg.add_task(
        _handle_slash_question,
        question=question,
        response_url=response_url,
        user_id=user_id,
        channel_id=channel_id,
    )
    return {"response_type": "ephemeral", "text": "📚 関連文書を検索中..."}


@app.post("/slack/interactive")
async def slack_interactive(request: Request, bg: BackgroundTasks) -> dict[str, Any]:
    """Slack requires a 2xx response within 3s. Ack immediately and do the
    DB write + message rewrite in a background task so a slow DB or expired
    response_url can never surface as a 500 to the user."""
    body = await request.body()
    _verify_slack(request, body)
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    payload = json.loads(form["payload"])

    if payload.get("type") != "block_actions":
        return {}

    actions = payload.get("actions", [])
    if not actions:
        return {}
    action = actions[0]
    action_id: str = action["action_id"]
    query_id_str: str = action["value"]
    user_id: str = payload["user"]["id"]
    response_url: str = payload["response_url"]
    original_message: dict = payload.get("message", {}) or {}

    rating = 1 if action_id == "feedback_up" else -1
    try:
        qid = UUID(query_id_str)
    except ValueError:
        log.warning("invalid query_id in interactive payload: %s", query_id_str)
        return {}

    bg.add_task(
        _handle_feedback_click,
        qid=qid,
        rating=rating,
        user_id=user_id,
        response_url=response_url,
        original_blocks=original_message.get("blocks", []) or [],
        original_text=original_message.get("text", ""),
    )
    return {}


async def _handle_feedback_click(
    *,
    qid: UUID,
    rating: int,
    user_id: str,
    response_url: str,
    original_blocks: list[dict],
    original_text: str,
) -> None:
    try:
        await asyncio.to_thread(_save_feedback, qid, rating=rating, slack_user=user_id)
    except Exception:
        log.exception("failed to save feedback for query_id=%s", qid)

    new_blocks = _strip_action_block(original_blocks)
    new_blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_<@{user_id}> が {'👍' if rating == 1 else '👎'} を記録しました_",
                }
            ],
        }
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                response_url,
                json={"replace_original": True, "blocks": new_blocks, "text": original_text},
            )
            if resp.status_code >= 400:
                log.warning("response_url returned %s: %s", resp.status_code, resp.text[:300])
    except Exception:
        log.exception("failed to update message via response_url")


# --------------------------------------------------------------------------------------
# Stage orchestration
# --------------------------------------------------------------------------------------


async def _handle_event_question(
    *, question: str, channel: str, thread_ts: str | None, user_id: str
) -> None:
    """Handles app_mention + DM. Posts an initial message and updates it through stages."""
    try:
        ack = await _slack().chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="📚 関連文書を検索中..."
        )
        ts: str = ack["ts"]

        async def update(
            text: str, blocks: list[dict] | None = None, *, final: bool = False
        ) -> None:
            # Mention/DM path is already public; `final` is accepted to keep the
            # callable signature uniform with the slash-command path.
            del final
            kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
            if blocks is not None:
                kwargs["blocks"] = blocks
            await _slack().chat_update(**kwargs)

        await _process_question(question=question, user_id=user_id, update=update)
    except Exception:
        log.exception("event handler failed")
        try:
            await _slack().chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="申し訳ありません、エラーが発生しました。管理者に連絡してください。",
            )
        except Exception:
            log.exception("failed to send error message")


async def _handle_slash_question(
    *, question: str, response_url: str, user_id: str, channel_id: str
) -> None:
    """Slash commands: progress updates stay ephemeral via response_url; the final
    answer is posted publicly to the channel (and the ephemeral is deleted) so
    that teammates in the channel can see and reuse the answer."""
    async with httpx.AsyncClient(timeout=10) as client:

        async def update(
            text: str, blocks: list[dict] | None = None, *, final: bool = False
        ) -> None:
            if final and channel_id:
                try:
                    await client.post(response_url, json={"delete_original": True})
                except Exception:
                    log.exception("failed to delete ephemeral progress message")
                kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
                if blocks is not None:
                    kwargs["blocks"] = blocks
                await _slack().chat_postMessage(**kwargs)
                return

            payload: dict[str, Any] = {
                "response_type": "ephemeral",
                "replace_original": True,
                "text": text,
            }
            if blocks is not None:
                payload["blocks"] = blocks
            await client.post(response_url, json=payload)

        try:
            await _process_question(question=question, user_id=user_id, update=update)
        except Exception:
            log.exception("slash handler failed")
            await update("申し訳ありません、エラーが発生しました。")


async def _process_question(*, question: str, user_id: str, update) -> None:
    """Slack-side adapter: delegates to the surface-agnostic pipeline orchestrator
    and renders the final answer as Slack blocks. Behaviour is unchanged from the
    prior inline implementation; the body now lives in `pipeline.run_pipeline`."""
    await run_pipeline_for_slack(
        question=question,
        user_id=user_id,
        update=update,
        insert_query=_insert_query,
        build_answer_blocks=_build_answer_blocks,
    )


# --------------------------------------------------------------------------------------
# Block builders
# --------------------------------------------------------------------------------------


def _build_answer_blocks(
    answer_text: str,
    chunks: list[RetrievedChunk],
    query_id: UUID | None,
    latency_ms: int,
    stats: RetrievalStats,
) -> list[dict]:
    mrkdwn = to_slack_mrkdwn(answer_text or "(no answer)")
    blocks: list[dict] = split_to_section_blocks(mrkdwn)

    if chunks:
        nums = assign_source_numbers(chunks)
        # Group by source number: one entry per unique doc, with deep-link headings
        # listed underneath. Numbers match the `[N]` citations the LLM emitted.
        grouped: dict[int, dict[str, Any]] = {}
        seen_headings: set[tuple[int, str]] = set()
        for n, c in zip(nums, chunks):
            entry = grouped.setdefault(
                n,
                {
                    "title": c.doc_title,
                    "url": c.doc_url,
                    "headings": [],
                },
            )
            heading = " › ".join(h for h in c.heading_path if h) if c.heading_path else ""
            if heading and (n, heading) not in seen_headings:
                seen_headings.add((n, heading))
                entry["headings"].append(heading)

        srcs: list[str] = []
        for n in sorted(grouped):
            entry = grouped[n]
            label = (
                f"<{entry['url']}|{entry['title']}>" if entry["url"] else entry["title"]
            )
            lines = [f"`[{n}]` {label}"]
            for h in entry["headings"][:3]:
                lines.append(f"      _› {h}_")
            srcs.append("\n".join(lines))

        if srcs:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "*出典*\n" + "\n".join(srcs)}],
                }
            )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "feedback_up",
                    "text": {"type": "plain_text", "text": "👍 役立った"},
                    "value": str(query_id),
                },
                {
                    "type": "button",
                    "action_id": "feedback_down",
                    "text": {"type": "plain_text", "text": "👎 改善が必要"},
                    "value": str(query_id),
                },
            ],
        }
    )

    process_line = (
        f"📚 検索 {stats.n_fused}件 ・ "
        f"🔍 評価 {stats.n_above_floor}件 ・ "
        f"✍️ 生成 {latency_ms / 1000:.1f}秒"
    )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": process_line}],
        }
    )
    return blocks


def _strip_action_block(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b.get("type") != "actions"]


# --------------------------------------------------------------------------------------
# Slack signature verification
# --------------------------------------------------------------------------------------


def _verify_slack(request: Request, body: bytes) -> None:
    s = _settings()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not ts or not sig:
        raise HTTPException(status_code=400, detail="missing slack signature headers")
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            raise HTTPException(status_code=403, detail="stale request")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad timestamp") from exc

    base = f"v0:{ts}:{body.decode('utf-8', errors='replace')}".encode()
    expected = (
        "v0="
        + hmac.new(s.slack_signing_secret.encode(), base, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=403, detail="bad signature")


def _strip_mention(text: str) -> str:
    parts = text.split(maxsplit=1)
    if parts and parts[0].startswith("<@") and parts[0].endswith(">"):
        return parts[1] if len(parts) > 1 else ""
    return text.strip()


# --------------------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------------------


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


def _save_feedback(query_id: UUID, *, rating: int, slack_user: str) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE queries SET feedback=%s, feedback_text=%s WHERE id=%s",
            (rating, f"slack:{slack_user}", query_id),
        )
        c.commit()
