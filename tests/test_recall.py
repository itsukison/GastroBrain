"""Exercise the actual FastAPI boundary and SQL against an isolated Postgres.

Install the dev group (pgserver bundles PostgreSQL); no production DB is used.
"""
import base64
import hashlib
import hmac
import json
import time
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import fasteners
import httpx
import pgserver
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from gastrobrain import recall_api as api, recall_worker as worker
from gastrobrain.auth import AuthUser, require_user
from gastrobrain.recall_client import RecallClient, RecallConfig, RecallError, verify_webhook

ROOT = Path(__file__).resolve().parents[1]
SECRET = "whsec_" + base64.b64encode(b"test-secret-not-production").decode()
CFG = RecallConfig("ap-northeast-1", "fake", SECRET, "https://api.example.com", "https://web.example.com", "worker-test")


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    temporary = tempfile.TemporaryDirectory(prefix="gb-pg-", dir="/tmp")
    folder = Path(temporary.name)
    pgserver.PostgresServer.runtime_path = folder
    pgserver.PostgresServer._lock = fasteners.InterProcessLock(folder / "server.lock")
    server = pgserver.get_server(folder / "db", cleanup_mode="delete")
    uri = server.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("CREATE SCHEMA auth; CREATE ROLE anon; CREATE ROLE authenticated; "
                  "CREATE TABLE auth.users(id uuid PRIMARY KEY,email text,deleted_at timestamptz,banned_until timestamptz); "
                  "CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS $$SELECT NULL::uuid$$; "
                  "CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql AS $$SELECT '{}'::jsonb$$; "
                  "CREATE TABLE queries(id uuid PRIMARY KEY);")
        for migration in ["002_web_chat.sql", "014_meetings.sql", "015_recall_runs.sql"]:
            c.execute((ROOT / "migrations" / migration).read_text())
    yield uri
    server.cleanup()
    temporary.cleanup()


@pytest.fixture
def env(database, monkeypatch):
    @contextmanager
    def connect():
        with psycopg.connect(database) as c:
            yield c
    monkeypatch.setattr(api, "conn", connect)
    monkeypatch.setattr(worker, "conn", connect)
    monkeypatch.setattr(api, "config", lambda: CFG)
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(recall_enabled=True))
    monkeypatch.setattr(worker, "kick", lambda: None)  # Explicit drain = process-crash/replay test.
    with connect() as c:
        c.execute("TRUNCATE auth.users,queries,meetings CASCADE")
    user = AuthUser(uuid4(), "tester@gastroduce-japan.co.jp")
    with connect() as c:
        c.execute("INSERT INTO auth.users(id,email) VALUES (%s,%s)", (user.user_id, user.email))
    fake = FakeRecall()
    monkeypatch.setattr(api, "client", lambda: fake)
    from gastrobrain.slack_app import app
    app.dependency_overrides[require_user] = lambda: user
    from gastrobrain import web_api
    monkeypatch.setattr(web_api, "_voice_vocab_terms", lambda email: ["ガストロ"])
    summaries = []
    def summarize(meeting_id):
        with connect() as c:
            count = c.execute("SELECT count(*) FROM meeting_segments WHERE meeting_id=%s", (meeting_id,)).fetchone()[0]
            summaries.append(count)
            c.execute("UPDATE meetings SET summary_status='ready',summary='test summary' WHERE id=%s", (meeting_id,))
    monkeypatch.setattr(web_api, "_summarize_meeting", summarize)
    with TestClient(app, raise_server_exceptions=False) as http:
        yield SimpleNamespace(http=http, db=connect, fake=fake, user=user, app=app, summaries=summaries)
    app.dependency_overrides.clear()


class FakeRecall:
    def __init__(self):
        self.bot_id, self.recording_id, self.transcript_id = uuid4(), uuid4(), uuid4()
        self.created, self.left = [], []
        self.ambiguous = False
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.segments = []
        self.transcript_ready = False

    def create_bot(self, run_id, meet_url, launch):
        self.created.append((run_id, meet_url, launch))
        if self.ambiguous:
            raise RecallError()
        return {"id": str(self.bot_id)}

    def find_run(self, run_id):
        return [self.bot(str(self.bot_id))] if self.created else []

    def bot(self, bot_id):
        return {"id": str(self.bot_id), "metadata": {"gastrobrain_run_id": self.created[0][0]},
                "status_changes": [], "recordings": [{"id": str(self.recording_id), "media_shortcuts": {
                    "transcript": {"id": str(self.transcript_id), "status": {"code": "done" if self.transcript_ready else "processing"}}}}]}

    def leave(self, bot_id):
        self.left.append(bot_id)

    def request(self, method, path):
        return {"started_at": self.started_at, "bot": {"id": str(self.bot_id)}}

    def transcript(self, transcript_id):
        return self.segments


def start(env):
    response = env.http.post("/v1/recall/runs", json={"meet_url": "https://meet.google.com/abc-defg-hij"})
    assert response.status_code == 202, response.text
    run = response.json()
    exchange = env.http.post("/v1/recall/bot/exchange", json={"token": env.fake.created[-1][2]})
    assert exchange.status_code == 200, exchange.text
    return run, {"Authorization": "Bearer " + exchange.json()["token"]}


def event(env, kind, data=None, event_id=None):
    payload = {"event": kind, "data": {"bot": {"id": str(env.fake.bot_id), "metadata": {"gastrobrain_run_id": env.fake.created[0][0]}},
        "recording": {"id": str(env.fake.recording_id)}, "transcript": {"id": str(env.fake.transcript_id)}, "data": data or {}}}
    raw = json.dumps(payload).encode()
    stamp, eid = str(int(time.time())), event_id or str(uuid4())
    signature = base64.b64encode(hmac.digest(base64.b64decode(SECRET[6:]), f"{eid}.{stamp}.".encode() + raw, "sha256")).decode()
    headers = {"webhook-id": eid, "webhook-timestamp": stamp, "webhook-signature": "v1," + signature, "Content-Type": "application/json"}
    response = env.http.post("/v1/recall/webhook", content=raw, headers=headers)
    assert response.status_code == 202, response.text
    return payload


def sql(env, query, params=()):
    with env.db() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def segment(text, relative):
    return {"participant": {"id": 7, "name": "参加者A"}, "words": [
        {"text": text, "start_timestamp": {"relative": relative}, "end_timestamp": {"relative": relative + 1}}]}


def test_run_grant_auth_and_rag_binding(env, monkeypatch):
    run, auth = start(env)
    assert env.http.post("/v1/recall/bot/exchange", json={"token": env.fake.created[0][2]}).status_code == 401
    assert env.http.get("/v1/recall/bot/context").status_code == 401
    assert env.http.get("/v1/recall/bot/context", headers={"Authorization": "Bearer wrong"}).status_code == 401
    context = env.http.get("/v1/recall/bot/context", headers=auth).json()
    assert context["meeting_id"] == run["meeting_id"] and not context["active"]
    assert env.http.post("/v1/recall/bot/ask", headers=auth, json={"question": "営業資料は？"}).status_code == 409
    event(env, "bot.in_call_recording")
    worker.tick()
    from gastrobrain import web_api
    calls = []
    async def voice_ask(body, user):
        calls.append((body, user))
        return {"answer": "はい", "citations": []}
    monkeypatch.setattr(web_api, "voice_ask", voice_ask)
    assert env.http.post("/v1/recall/bot/ask", headers=auth, json={"question": "営業資料は？"}).status_code == 200
    assert calls[0][0].conversation_id == UUID(context["conversation_id"])
    assert calls[0][1] == env.user
    assert env.http.post("/v1/recall/bot/ask", headers=auth, json={"question": "?", "conversation_id": str(uuid4())}).status_code == 422
    sql(env, "DELETE FROM meeting_participants WHERE meeting_id=%s", (run["meeting_id"],))
    assert env.http.get("/v1/recall/bot/context", headers=auth).status_code == 401


def test_ambiguous_and_duplicate_starts_never_recreate(env):
    env.fake.ambiguous = True
    run, auth = start(env)
    assert run["state"] == "uncertain"
    duplicate = env.http.post("/v1/recall/runs", json={"meet_url": "https://meet.google.com/abc-defg-hij"})
    assert duplicate.json()["id"] == run["id"] and len(env.fake.created) == 1
    worker.tick()
    assert sql(env, "SELECT bot_id,state FROM recall_runs")[0] == {"bot_id": env.fake.bot_id, "state": "joining"}
    env.app.dependency_overrides[require_user] = lambda: AuthUser(uuid4(), "other@gastroduce-japan.co.jp")
    assert env.http.post("/v1/recall/runs", json={"meet_url": "https://meet.google.com/abc-defg-hij"}).status_code == 409
    assert env.http.post(f"/v1/recall/runs/{run['id']}/stop").status_code == 404


def test_durable_replay_transcript_tail_and_summary_once(env):
    run, auth = start(env)
    event(env, "bot.in_call_recording")
    event(env, "transcript.data", segment("後半", 20), "same-event")
    event(env, "transcript.data", segment("後半", 20), "same-event")
    # A distinct delivery of the same caption still deduplicates.
    event(env, "transcript.data", segment("後半", 20))
    event(env, "bot.done")
    event(env, "bot.in_call_recording")  # late lifecycle event must not reopen
    worker.tick()
    assert sql(env, "SELECT state FROM recall_runs")[0]["state"] == "finishing"
    assert env.summaries == []
    assert env.http.get("/v1/recall/bot/context", headers=auth).status_code == 401
    env.fake.segments = [segment("前半", 1), segment("後半", 20), segment("最後", 25)]
    env.fake.transcript_ready = True
    event(env, "transcript.done")
    sql(env, "UPDATE recall_runs SET next_check_at=now()")
    worker.tick()
    assert [s["text"] for s in sql(env, "SELECT text FROM meeting_segments ORDER BY seq")] == ["前半", "後半", "最後"]
    assert env.summaries == [3]
    event(env, "bot.done")
    event(env, "transcript.done")
    worker.tick()
    assert env.summaries == [3]
    assert sql(env, "SELECT state FROM recall_runs")[0]["state"] == "done"


def test_chat_claim_crash_expiry_and_state_compare_and_set(env):
    run, auth = start(env)
    event(env, "bot.in_call_recording")
    fresh = {"timestamp": {"absolute": datetime.now(timezone.utc).isoformat()}, "data": {"text": "商談AI 資料を教えて"}}
    stale = {"timestamp": {"absolute": "2000-01-01T00:00:00Z"}, "data": {"text": "古い質問"}}
    event(env, "participant_events.chat_message", fresh, "fresh")
    event(env, "participant_events.chat_message", stale, "old")
    worker.tick()
    first = env.http.post("/v1/recall/bot/control", headers=auth, json={"healthy": True}).json()
    assert [c["id"] for c in first["commands"]] == ["fresh"]
    # Crash after claim before ack: the new page must not ask it again.
    second = env.http.post("/v1/recall/bot/control", headers=auth, json={"healthy": True}).json()
    assert second["commands"] == []
    sql(env, "UPDATE meetings SET agent_state='open'")
    stale_poll = env.http.post("/v1/recall/bot/control", headers=auth,
        json={"healthy": True, "local_state": "asleep", "observed_state": "asleep"}).json()
    assert stale_poll["agent_state"] == "open"
    idle_expiry = env.http.post("/v1/recall/bot/control", headers=auth,
        json={"healthy": True, "local_state": "asleep", "observed_state": "open", "acknowledgements": ["fresh"]}).json()
    assert idle_expiry["agent_state"] == "asleep"
    assert sql(env, "SELECT acked_at FROM recall_commands WHERE id='fresh'")[0]["acked_at"]


def test_stop_watchdog_and_expiry(env):
    run, auth = start(env)
    event(env, "bot.in_call_recording")
    worker.tick()
    sql(env, "UPDATE recall_runs SET admitted_at=now()-interval '4 minutes',next_check_at=now()")
    worker.tick()
    assert env.fake.left == [str(env.fake.bot_id)]
    assert env.http.get("/v1/recall/bot/context", headers=auth).status_code == 401
    assert sql(env, "SELECT problem FROM recall_runs")[0]["problem"] == "voice_unhealthy"


def test_grant_expiry_and_manual_stop(env):
    run, auth = start(env)
    sql(env, "UPDATE recall_runs SET expires_at=now()-interval '1 second'")
    assert env.http.get("/v1/recall/bot/context", headers=auth).status_code == 401
    assert env.http.post(f"/v1/recall/runs/{run['id']}/stop").status_code == 202
    worker.tick()
    assert env.fake.left == [str(env.fake.bot_id)]


def test_final_transcript_failure_is_not_a_successful_empty_summary(env):
    run, auth = start(env)
    event(env, "bot.done")
    event(env, "transcript.failed")
    worker.tick()
    assert env.summaries == []
    assert sql(env, "SELECT state,problem FROM recall_runs")[0] == {"state": "failed", "problem": "transcript_incomplete"}
    assert sql(env, "SELECT summary_status FROM meetings")[0]["summary_status"] == "failed"


def test_early_admission_during_create_is_not_regressed(env, monkeypatch):
    original = env.fake.create_bot
    def early(*args):
        result = original(*args)
        event(env, "bot.in_call_recording")
        worker.drain_events(env.fake)
        return result
    monkeypatch.setattr(env.fake, "create_bot", early)
    start(env)
    assert sql(env, "SELECT state FROM recall_runs")[0]["state"] == "live"


def test_signatures_and_direct_database_roles(env):
    assert env.http.post("/v1/recall/webhook", json={"event": "bot.done"}).status_code == 400
    assert env.http.post("/v1/recall/drain").status_code == 401
    assert env.http.post("/v1/recall/drain", headers={"Authorization": "Bearer worker-test"}).status_code == 200
    for role in ("anon", "authenticated"):
        with env.db() as c:
            c.execute(f"SET LOCAL ROLE {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT * FROM recall_runs")
            c.rollback()
    with pytest.raises(ValueError):
        verify_webhook(b"{}", {"webhook-id": "a", "webhook-timestamp": "1", "webhook-signature": "v1,eA=="}, SECRET)


def test_transport_create_is_not_retried_and_payload_is_scoped():
    requests = []
    def receive(request):
        requests.append(request)
        return httpx.Response(503)
    client = RecallClient(CFG, transport=httpx.MockTransport(receive))
    with pytest.raises(RecallError):
        client.create_bot("run", "https://meet.google.com/abc-defg-hij", "short-lived")
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["recording_config"]["transcript"]["provider"] == {"meeting_captions": {"language_code": "ja"}}
    assert payload["metadata"] == {"gastrobrain_run_id": "run"}
    assert "#launch=" in payload["output_media"]["camera"]["config"]["url"]
