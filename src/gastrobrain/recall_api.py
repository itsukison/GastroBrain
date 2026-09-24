"""Recall pilot API: human auth for runs, single-run grants for the bot page."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from psycopg.rows import dict_row
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from gastrobrain.auth import AuthUser, require_user
from gastrobrain.config import get_settings
from gastrobrain.db import conn
from gastrobrain.recall_client import RecallClient, RecallConfig, RecallError, digest, verify_webhook

router = APIRouter(prefix="/v1/recall")


def config() -> RecallConfig:
    s = get_settings()
    if not s.recall_enabled:
        raise HTTPException(503, "Recall pilot is not enabled")
    try:
        return RecallConfig.from_settings(s)
    except ValueError as exc:
        raise HTTPException(503, "Recall runtime is not configured") from exc


def client() -> RecallClient:
    return RecallClient(config())


def now():
    return datetime.now(timezone.utc)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meet_url: str
    title: str = Field(default="商談AI テスト会議", min_length=1, max_length=200)
    attendees: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("meet_url")
    @classmethod
    def url(cls, value):
        if not re.fullmatch(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", value):
            raise ValueError("Use a Google Meet URL without query parameters")
        return value

    @field_validator("attendees")
    @classmethod
    def emails(cls, values):
        if any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v) or len(v) > 320 for v in values):
            raise ValueError("Invalid attendee email")
        return sorted(set(v.lower() for v in values))


def start_run(body: StartBody, user: AuthUser):
    cfg = config()
    if not user.email or not user.email.lower().endswith("@gastroduce-japan.co.jp"):
        raise HTTPException(403, "A company operator is required")
    run_id, meeting_id, conversation_id = uuid4(), uuid4(), uuid4()
    launch = secrets.token_urlsafe(32)
    with conn() as c, c.cursor(row_factory=dict_row) as cur:
        # Serialize competing starts even before the unique active run exists.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (body.meet_url,))
        cur.execute("SELECT id, meeting_id, user_id, state FROM recall_runs WHERE meet_url=%s "
                    "AND state IN ('creating','uncertain','joining','live','stopping')", (body.meet_url,))
        existing = cur.fetchone()
        if existing:
            if existing["user_id"] != user.user_id:
                raise HTTPException(409, "This meeting already has an active bot")
            return {k: existing[k] for k in ("id", "meeting_id", "state")}
        cur.execute("INSERT INTO meetings(id,google_event_id,title,meet_url,scheduled_at,status) "
                    "VALUES (%s,%s,%s,%s,now(),'joining')",
                    (meeting_id, f"recall:{run_id}", body.title, body.meet_url))
        for email in sorted(set(body.attendees + [user.email.lower()])):
            cur.execute("INSERT INTO meeting_participants(meeting_id,email,is_organizer) VALUES (%s,%s,%s)",
                        (meeting_id, email, email == user.email.lower()))
        cur.execute("INSERT INTO conversations(id,user_id,title,meeting_id) VALUES (%s,%s,%s,%s)",
                    (conversation_id, user.user_id, body.title, meeting_id))
        cur.execute("INSERT INTO recall_runs(id,meeting_id,user_id,conversation_id,meet_url,launch_hash,"
                    "launch_expires_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (run_id, meeting_id, user.user_id, conversation_id, body.meet_url, digest(launch),
                     now() + timedelta(minutes=15), now() + timedelta(seconds=cfg.max_seconds + 600)))
        c.commit()  # Durable intent before the external side effect.
    try:
        bot = client().create_bot(str(run_id), body.meet_url, launch)
        bot_id = UUID(bot["id"])
    except (RecallError, KeyError, ValueError) as exc:
        refused = isinstance(exc, RecallError) and exc.status in {400, 401, 403, 422}
        with conn() as c:
            c.execute("UPDATE recall_runs SET state=%s,problem=%s,updated_at=now() WHERE id=%s AND state='creating'",
                      ("failed" if refused else "uncertain", "create_refused" if refused else "create_unconfirmed", run_id))
            if refused:
                c.execute("UPDATE meetings SET status='failed',summary_status='failed' WHERE id=%s", (meeting_id,))
            c.commit()
        # A timeout might already have created the bot. Worker reconciles by metadata.
        return {"id": run_id, "meeting_id": meeting_id, "state": "failed" if refused else "uncertain"}
    with conn() as c:
        c.execute("UPDATE recall_runs SET bot_id=%s,state=CASE WHEN state IN ('creating','uncertain') "
                  "THEN 'joining' ELSE state END,updated_at=now() WHERE id=%s", (bot_id, run_id))
        c.commit()
    return {"id": run_id, "meeting_id": meeting_id, "state": "joining"}


@router.post("/runs", status_code=202)
async def start(body: StartBody, user: AuthUser = Depends(require_user)):
    return await asyncio.to_thread(start_run, body, user)


def owned_run(run_id: UUID, user: AuthUser):
    with conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT r.id,r.meeting_id,r.state,r.problem,r.created_at,r.ended_at "
                    "FROM recall_runs r JOIN meetings m ON m.id=r.meeting_id "
                    "WHERE r.id=%s AND r.user_id=%s AND m.deleted_at IS NULL",
                    (run_id, user.user_id))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        return run


@router.get("/runs")
async def list_runs(user: AuthUser = Depends(require_user)):
    def read():
        with conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT r.id,r.meeting_id,r.state,r.problem,m.title FROM recall_runs r "
                        "JOIN meetings m ON m.id=r.meeting_id WHERE r.user_id=%s AND m.deleted_at IS NULL "
                        "ORDER BY r.created_at DESC LIMIT 20", (user.user_id,))
            return {"runs": cur.fetchall()}
    if not get_settings().recall_enabled:
        return {"runs": [], "enabled": False}
    return {**await asyncio.to_thread(read), "enabled": True}


@router.post("/runs/{run_id}/stop", status_code=202)
async def stop(run_id: UUID, tasks: BackgroundTasks, user: AuthUser = Depends(require_user)):
    from gastrobrain.recall_worker import kick
    def write():
        owned_run(run_id, user)
        with conn() as c:
            c.execute("UPDATE recall_runs SET state='stopping',launch_hash=NULL,session_hash=NULL,"
                      "ended_at=COALESCE(ended_at,now()),next_check_at=now(),updated_at=now() WHERE id=%s "
                      "AND state IN ('creating','uncertain','joining','live')", (run_id,))
            c.commit()
        return {"status": "stopping"}
    result = await asyncio.to_thread(write)
    tasks.add_task(kick)
    return result


class LaunchBody(BaseModel):
    token: str = Field(min_length=30, max_length=100)


@router.post("/bot/exchange")
async def exchange(body: LaunchBody):
    config()
    def consume():
        session = secrets.token_urlsafe(32)
        with conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("UPDATE recall_runs SET launch_hash=NULL,session_hash=%s,heartbeat_at=now() "
                        "WHERE launch_hash=%s AND launch_expires_at>now() "
                        "AND state IN ('creating','uncertain','joining','live') "
                        "RETURNING id,meeting_id,conversation_id,expires_at",
                        (digest(session), digest(body.token)))
            run = cur.fetchone()
            if not run:
                raise HTTPException(401, "Launch expired or already used")
            c.commit()
        return {**run, "token": session}
    return await asyncio.to_thread(consume)


def authenticate_bot(authorization: str | None):
    config()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bot session required")
    token = authorization[7:]
    with conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT r.*,u.email,m.agent_state FROM recall_runs r "
                    "JOIN auth.users u ON u.id=r.user_id "
                    "JOIN meetings m ON m.id=r.meeting_id "
                    "JOIN conversations cv ON cv.id=r.conversation_id AND cv.user_id=r.user_id "
                    "AND cv.meeting_id=r.meeting_id "
                    "WHERE r.session_hash=%s AND r.expires_at>now() "
                    "AND r.state IN ('creating','uncertain','joining','live') "
                    "AND m.deleted_at IS NULL AND cv.deleted_at IS NULL "
                    "AND u.deleted_at IS NULL AND (u.banned_until IS NULL OR u.banned_until<now()) "
                    "AND EXISTS (SELECT 1 FROM meeting_participants p WHERE p.meeting_id=r.meeting_id "
                    "AND p.email=lower(u.email))", (digest(token),))
        run = cur.fetchone()
        if not run:
            raise HTTPException(401, "Bot session ended or access revoked")
        return run


async def bot_session(authorization: str | None = Header(default=None)):
    return await asyncio.to_thread(authenticate_bot, authorization)


@router.get("/bot/context")
async def context(run=Depends(bot_session)):
    from gastrobrain.web_api import _voice_vocab_terms
    return {"id": run["id"], "meeting_id": run["meeting_id"],
            "conversation_id": run["conversation_id"], "active": run["state"] == "live",
            "terms": await asyncio.to_thread(_voice_vocab_terms, run["email"])}


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    utterance: str | None = Field(default=None, max_length=1000)


@router.post("/bot/ask")
async def ask(body: AskBody, run=Depends(bot_session)):
    if run["state"] != "live":
        raise HTTPException(409, "Meeting is not live")
    from gastrobrain.web_api import VoiceAskBody, voice_ask
    return await voice_ask(VoiceAskBody(conversation_id=run["conversation_id"], **body.model_dump()),
                           AuthUser(run["user_id"], run["email"]))


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_state: Literal["asleep", "open"] = "asleep"
    observed_state: Literal["asleep", "open"] | None = None
    healthy: bool = False
    acknowledgements: list[str] = Field(default_factory=list, max_length=50)


@router.post("/bot/control")
async def control(body: ControlBody, tasks: BackgroundTasks, run=Depends(bot_session)):
    from gastrobrain.recall_worker import kick
    def sync():
        with conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT state FROM recall_runs WHERE id=%s AND session_hash IS NOT NULL FOR UPDATE", (run["id"],))
            current = cur.fetchone()
            if not current or current["state"] not in {'creating', 'uncertain', 'joining', 'live'}:
                raise HTTPException(401, "Bot session ended")
            run["state"] = current["state"]
            cur.execute("SELECT agent_state FROM meetings WHERE id=%s FOR UPDATE", (run["meeting_id"],))
            remote = cur.fetchone()["agent_state"]
            # Compare-and-set: a newer web control always wins this tick.
            if body.observed_state == remote and body.local_state != remote:
                remote = body.local_state
                cur.execute("UPDATE meetings SET agent_state=%s WHERE id=%s", (remote, run["meeting_id"]))
            cur.execute("UPDATE recall_runs SET heartbeat_at=now(),healthy_at=CASE WHEN %s THEN now() "
                        "ELSE healthy_at END WHERE id=%s", (body.healthy, run["id"]))
            if body.acknowledgements:
                cur.execute("UPDATE recall_commands SET acked_at=now() WHERE run_id=%s AND id=ANY(%s) "
                            "AND claimed_at IS NOT NULL", (run["id"], body.acknowledgements))
            commands = []
            if body.healthy and run["state"] == "live":
                cur.execute("UPDATE recall_commands SET claimed_at=now() WHERE id IN "
                            "(SELECT id FROM recall_commands WHERE run_id=%s AND claimed_at IS NULL "
                            "AND expires_at>now() ORDER BY created_at LIMIT 20 FOR UPDATE SKIP LOCKED) "
                            "RETURNING id,text,created_at", (run["id"],))
                commands = sorted(cur.fetchall(), key=lambda c: c["created_at"])
            # Claims are at-most-once. On crash-before-ack, do not repeat a question aloud.
            c.commit()
            return {"active": run["state"] == "live", "agent_state": remote, "commands": commands}
    result = await asyncio.to_thread(sync)
    tasks.add_task(kick)
    return result


class SpokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=10000)
    spoken_at: AwareDatetime
    ended_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_interval(self):
        if self.ended_at is not None and not (timedelta(0) <= self.ended_at - self.spoken_at <= timedelta(minutes=10)):
            raise ValueError("Invalid playback interval")
        return self


@router.post("/bot/spoken")
async def spoken(body: SpokenBody, run=Depends(bot_session)):
    from gastrobrain.recall_transcript import insert_source, reconcile_speech
    def insert():
        with conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM recall_runs WHERE id=%s FOR UPDATE", (run["id"],))
            locked = cur.fetchone()
            if not locked or locked["state"] not in {"creating", "uncertain", "joining", "live"} or locked["session_hash"] is None:
                raise HTTPException(401, "Bot session ended")
            insert_source(cur, locked, "spoken:" + body.item_id, "商談AI", body.text, body.spoken_at,
                          source="spoken", ended_at=body.ended_at, raw=body.model_dump(mode="json"))
            reconcile_speech(cur, locked)
            c.commit()
    await asyncio.to_thread(insert)
    return {"ok": True}


@router.post("/webhook", status_code=202)
async def webhook(request: Request, tasks: BackgroundTasks):
    from gastrobrain.recall_worker import kick
    cfg = config()
    # Bound memory before reading an untrusted public request.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1_000_000:
            raise HTTPException(413, "Webhook too large")
    try:
        event_id = verify_webhook(bytes(raw), request.headers, cfg.verification_secret)
        event = json.loads(raw)
        bot_id = UUID(event["data"]["bot"]["id"])
        if not isinstance(event.get("event"), str):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid Recall event") from None
    def accept():
        with conn() as c:
            c.execute("INSERT INTO recall_events(id,bot_id,payload) VALUES (%s,%s,%s::jsonb) "
                      "ON CONFLICT(id) DO NOTHING", (event_id, bot_id, json.dumps(event)))
            c.commit()
    await asyncio.to_thread(accept)
    tasks.add_task(kick)
    return {"accepted": True}


@router.post("/drain")
async def drain(authorization: str | None = Header(default=None)):
    expected = "Bearer " + config().worker_token
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Worker authentication required")
    from gastrobrain.recall_worker import tick
    return await asyncio.to_thread(tick)
