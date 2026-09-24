"""Durable Recall inbox and run recovery, driven by webhooks and /drain.

Row locks serialize each run. No session advisory locks (transaction poolers
must work), and failed work stays in Postgres for the next scheduled drain.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from psycopg.rows import dict_row

from gastrobrain.db import conn
from gastrobrain.recall_client import RecallError, caption_key
from gastrobrain.recall_transcript import insert_source, reconcile_speech, replace_live

log = logging.getLogger(__name__)


def clock():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not value:
        raise ValueError("Missing source timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must have an offset")
    return result


def import_segment(cur, run, transcript, segment, *, final_index=None):
    # A completed download supersedes live events, including late deliveries.
    if final_index is None and transcript["imported_at"]:
        return
    words = segment.get("words") or []
    if not words:
        return
    participant = segment.get("participant") or {}
    speaker = participant.get("name") or "参加者"

    def word_time(value):
        if value.get("absolute"):
            return timestamp(value["absolute"])
        return transcript["started_at"] + timedelta(seconds=float(value["relative"]))

    started = word_time(words[0].get("start_timestamp") or {})
    end = words[-1].get("end_timestamp")
    ended = max(started, word_time(end)) if end else None
    source = "live" if final_index is None else "final"
    # Final index preserves genuinely repeated identical captions; transaction
    # rollback plus imported_at makes full-download replay idempotent.
    key = ("live:" + caption_key(str(transcript["id"]), segment) if final_index is None
           else f"final:{transcript['id']}:{final_index}")
    insert_source(cur, run, key, speaker, "".join(w.get("text", "") for w in words), started,
                  source=source, ended_at=ended, transcript_id=transcript["id"],
                  participant_id=str(participant["id"]) if participant.get("id") is not None else None,
                  raw=segment)


def transcript_row(cur, run, transcript_id, recording_id, client):
    cur.execute("SELECT * FROM recall_transcripts WHERE id=%s", (transcript_id,))
    row = cur.fetchone()
    if row:
        if row["run_id"] != run["id"]:
            raise ValueError("Transcript run mismatch")
        return row
    recording = client.request("GET", f"/api/v1/recording/{recording_id}/")
    if str((recording.get("bot") or {}).get("id")) != str(run["bot_id"]):
        raise ValueError("Recording bot mismatch")
    started = timestamp(recording["started_at"]) if recording.get("started_at") else None
    cur.execute("INSERT INTO recall_transcripts(id,run_id,recording_id,started_at) VALUES (%s,%s,%s,%s) RETURNING *",
                (transcript_id, run["id"], recording_id, started))
    return cur.fetchone()


def finish(cur, run, problem=None):
    cur.execute("UPDATE recall_runs SET state='finishing',ended_at=COALESCE(ended_at,now()),"
                "session_hash=NULL,launch_hash=NULL,problem=COALESCE(%s,problem),next_check_at=now(),updated_at=now() "
                "WHERE id=%s AND state NOT IN ('done','failed')", (problem, run["id"]))
    cur.execute("UPDATE meetings SET status='ended',ended_at=COALESCE(ended_at,now()),agent_state='asleep' WHERE id=%s",
                (run["meeting_id"],))


def handle_event(cur, run, event, client):
    envelope = event["payload"]["data"]
    kind = event["payload"]["event"]
    data = envelope.get("data") or {}
    if kind in {"bot.in_call_recording", "bot.in_call_not_recording"}:
        cur.execute("UPDATE recall_runs SET state='live',admitted_at=COALESCE(admitted_at,now()),updated_at=now() "
                    "WHERE id=%s AND state IN ('creating','uncertain','joining','live') RETURNING id", (run["id"],))
        if cur.fetchone():
            cur.execute("UPDATE meetings SET status='live',started_at=COALESCE(started_at,now()) WHERE id=%s", (run["meeting_id"],))
    elif kind in {"bot.call_ended", "bot.done", "bot.fatal"}:
        finish(cur, run, "bot_fatal" if kind == "bot.fatal" else None)
    elif kind == "participant_events.chat_message":
        text = (data.get("data") or {}).get("text", "")
        sent = timestamp(data["timestamp"]["absolute"])
        if text and len(text) <= 4000 and sent <= clock() + timedelta(seconds=30):
            cur.execute("INSERT INTO recall_commands(id,run_id,text,created_at,expires_at) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (event["id"], run["id"], text, sent, sent + timedelta(seconds=30)))
    elif kind in {"transcript.data", "transcript.done", "transcript.failed"}:
        tid, rid = UUID(envelope["transcript"]["id"]), UUID(envelope["recording"]["id"])
        row = transcript_row(cur, run, tid, rid, client)
        if kind == "transcript.data":
            import_segment(cur, run, row, data)
            reconcile_speech(cur, run)
        else:
            cur.execute("UPDATE recall_transcripts SET ready=%s,failed=%s WHERE id=%s",
                        (kind == "transcript.done", kind == "transcript.failed", tid))
    elif kind == "recording.failed":
        cur.execute("UPDATE recall_runs SET problem='recording_failed' WHERE id=%s", (run["id"],))


def drain_events(client, limit=30):
    count = 0
    for _ in range(limit):
        with conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM recall_events WHERE processed_at IS NULL AND available_at<=now() "
                        "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED")
            event = cur.fetchone()
            if not event:
                break
            try:
                with c.transaction():  # Savepoint: a malformed event cannot poison the inbox.
                    metadata = (event["payload"]["data"]["bot"].get("metadata") or {})
                    raw_id = metadata.get("gastrobrain_run_id")
                    run_id = UUID(raw_id) if raw_id else None
                    cur.execute("SELECT * FROM recall_runs WHERE bot_id=%s OR (id=%s AND bot_id IS NULL) FOR UPDATE",
                                (event["bot_id"], run_id))
                    run = cur.fetchone()
                    if run:
                        if run["bot_id"] is None:
                            cur.execute("UPDATE recall_runs SET bot_id=%s WHERE id=%s", (event["bot_id"], run["id"]))
                            run["bot_id"] = event["bot_id"]
                        handle_event(cur, run, event, client)
                    cur.execute("UPDATE recall_events SET processed_at=now(),error_code=NULL WHERE id=%s", (event["id"],))
                    count += 1
            except Exception as exc:
                # Never log event payloads, signed URLs, captions or credentials.
                code = type(exc).__name__
                delay = min(300, 2 ** min(event["attempts"] + 1, 8))
                cur.execute("UPDATE recall_events SET attempts=attempts+1,error_code=%s,"
                            "available_at=now()+(%s * interval '1 second') WHERE id=%s", (code, delay, event["id"]))
                log.warning("Recall inbox retry: %s", code)
            c.commit()
    return count


def discover_transcripts(cur, run, client):
    bot = client.bot(str(run["bot_id"]))
    for recording in bot.get("recordings", []):
        transcript = (recording.get("media_shortcuts") or {}).get("transcript")
        if not transcript:
            continue
        row = transcript_row(cur, run, UUID(transcript["id"]), UUID(recording["id"]), client)
        status = (transcript.get("status") or {}).get("code")
        cur.execute("UPDATE recall_transcripts SET ready=ready OR %s,failed=failed OR %s WHERE id=%s",
                    (status == "done", status == "failed", row["id"]))


def maintain(cur, run, client):
    now = clock()
    if run["state"] in {"creating", "uncertain", "stopping"} and not run["bot_id"]:
        # Recovery after a create timeout/process crash. Never issue another create.
        bots = client.find_run(str(run["id"]))
        if len(bots) > 1:
            for bot in bots:
                client.leave(bot["id"])
            cur.execute("UPDATE recall_runs SET problem='duplicate_bots',state='failed',session_hash=NULL,launch_hash=NULL WHERE id=%s", (run["id"],))
            cur.execute("UPDATE meetings SET status='failed',summary_status='failed' WHERE id=%s", (run["meeting_id"],))
            return
        if bots:
            run["bot_id"] = UUID(bots[0]["id"])
            state = "stopping" if run["state"] == "stopping" else "joining"
            cur.execute("UPDATE recall_runs SET bot_id=%s,state=%s,problem=NULL WHERE id=%s", (run["bot_id"], state, run["id"]))
            # Reconcile the snapshot only for ambiguous creation, not normal polling.
            codes = [s.get("code") for s in bots[0].get("status_changes", [])]
            if any(s in codes for s in ("done", "fatal", "call_ended")):
                finish(cur, run)
            elif state != "stopping" and "in_call_recording" in codes:
                handle_event(cur, run, {"payload": {"event": "bot.in_call_recording", "data": {}}}, client)
        elif now - run["created_at"] > timedelta(minutes=15):
            # Keep the uncertain intent available for later operator reconciliation.
            cur.execute("UPDATE recall_runs SET state='uncertain',problem='create_unresolved' WHERE id=%s", (run["id"],))
        return

    active = run["state"] in {"joining", "live"}
    unhealthy = run["admitted_at"] and now - (run["healthy_at"] or run["admitted_at"]) > timedelta(seconds=180)
    if active and (now >= run["expires_at"] or unhealthy or
                   (not run["admitted_at"] and now - run["created_at"] > timedelta(minutes=12))):
        cur.execute("UPDATE recall_runs SET state='stopping',session_hash=NULL,launch_hash=NULL,"
                    "problem=%s WHERE id=%s", ("voice_unhealthy" if unhealthy else "deadline", run["id"]))
        run["state"] = "stopping"
    if run["state"] == "stopping" and run["bot_id"]:
        try:
            client.leave(str(run["bot_id"]))
        except RecallError as exc:
            if exc.status not in {400, 404, 409}:
                raise
            snapshot = client.bot(str(run["bot_id"]))
            if not any(s.get("code") in {"done", "fatal", "call_ended"} for s in snapshot.get("status_changes", [])):
                raise
        finish(cur, run)
    elif run["state"] == "live":
        cur.execute("SELECT EXISTS(SELECT 1 FROM recall_captions WHERE run_id=%s AND source_key NOT LIKE 'spoken:%%') AS present", (run["id"],))
        missing = not cur.fetchone()["present"] and now - run["admitted_at"] > timedelta(minutes=2)
        cur.execute("UPDATE recall_runs SET problem=CASE WHEN %s THEN 'captions_not_received' "
                    "WHEN problem='captions_not_received' THEN NULL ELSE problem END WHERE id=%s", (missing, run["id"]))
    elif run["state"] == "finishing":
        discover_transcripts(cur, run, client)
        cur.execute("SELECT * FROM recall_transcripts WHERE run_id=%s", (run["id"],))
        transcripts = cur.fetchall()
        failed = any(t["failed"] for t in transcripts)
        for transcript in transcripts:
            if transcript["ready"] and not transcript["imported_at"]:
                segments = client.transcript(str(transcript["id"]))
                if not isinstance(segments, list):
                    raise ValueError("Invalid final transcript")
                if not any(s.get("words") for s in segments):
                    cur.execute("SELECT EXISTS(SELECT 1 FROM recall_captions WHERE transcript_id=%s AND source='live') AS present", (transcript["id"],))
                    if cur.fetchone()["present"]:
                        raise ValueError("Empty final transcript would erase live evidence")
                replace_live(cur, transcript)
                for index, segment in enumerate(segments):
                    import_segment(cur, run, transcript, segment, final_index=index)
                reconcile_speech(cur, run)
                cur.execute("UPDATE recall_transcripts SET imported_at=now() WHERE id=%s", (transcript["id"],))
        cur.execute("SELECT count(*) AS n FROM recall_transcripts WHERE run_id=%s AND imported_at IS NULL", (run["id"],))
        pending = cur.fetchone()["n"]
        if transcripts and not pending and not failed:
            cur.execute("SELECT EXISTS(SELECT 1 FROM recall_events WHERE bot_id=%s AND processed_at IS NULL) AS pending", (run["bot_id"],))
            if cur.fetchone()["pending"]:
                return
            cur.execute("SELECT count(*) AS n FROM meeting_segments WHERE meeting_id=%s", (run["meeting_id"],))
            if not cur.fetchone()["n"]:
                failed = True
            else:
                # Final download can add earlier speech; order the existing record
                # chronologically before the shared summary/meeting Q&A reads it.
                cur.execute("UPDATE meeting_segments SET seq=-seq WHERE meeting_id=%s", (run["meeting_id"],))
                cur.execute("WITH ordered AS (SELECT id,row_number() OVER (ORDER BY spoken_at,id) AS n "
                            "FROM meeting_segments WHERE meeting_id=%s) UPDATE meeting_segments s SET seq=o.n "
                            "FROM ordered o WHERE s.id=o.id", (run["meeting_id"],))
                return "summarize"
        if failed or now - run["ended_at"] > timedelta(minutes=15):
            cur.execute("UPDATE recall_runs SET state='failed',problem='transcript_incomplete' WHERE id=%s", (run["id"],))
            cur.execute("UPDATE meetings SET summary_status='failed' WHERE id=%s AND summary_status!='ready'", (run["meeting_id"],))


def summarize_run(run_id):
    from gastrobrain.web_api import _summarize_meeting
    with conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM recall_runs WHERE id=%s AND state='finishing' FOR UPDATE SKIP LOCKED", (run_id,))
        run = cur.fetchone()
        if not run:
            return
        cur.execute("SELECT summary_status FROM meetings WHERE id=%s", (run["meeting_id"],))
        if cur.fetchone()["summary_status"] != "ready":
            _summarize_meeting(run["meeting_id"])
        cur.execute("UPDATE recall_runs SET state='done',updated_at=now() WHERE id=%s", (run_id,))
        c.commit()


def tick():
    from gastrobrain.recall_api import client
    transport = client()
    count = drain_events(transport)
    summaries = []
    deadline = time.monotonic() + 40
    with conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM recall_runs WHERE state NOT IN ('done','failed') AND next_check_at<=now() ORDER BY next_check_at LIMIT 20")
        ids = [r["id"] for r in cur.fetchall()]
    for run_id in ids:
        if time.monotonic() > deadline:
            break
        with conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM recall_runs WHERE id=%s AND next_check_at<=now() FOR UPDATE SKIP LOCKED", (run_id,))
            run = cur.fetchone()
            if not run:
                continue
            try:
                with c.transaction():
                    if maintain(cur, run, transport) == "summarize":
                        summaries.append(run_id)
            except Exception as exc:
                log.warning("Recall run retry: %s", type(exc).__name__)
                cur.execute("UPDATE recall_runs SET problem='recovery_pending' WHERE id=%s", (run_id,))
            cur.execute("UPDATE recall_runs SET next_check_at=now()+interval '30 seconds' WHERE id=%s", (run_id,))
            c.commit()
    for run_id in summaries:
        try:
            summarize_run(run_id)
        except Exception as exc:
            log.warning("Recall summary retry: %s", type(exc).__name__)
    return {"processed": count}


def kick():
    """Best-effort prompt processing; the external minute drain is mandatory."""
    try:
        tick()
    except Exception as exc:
        log.warning("Recall drain deferred: %s", type(exc).__name__)
