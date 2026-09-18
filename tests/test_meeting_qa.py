"""Regression coverage for meeting evidence, mixed search and access boundaries."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from gastrobrain import generate, llm, pipeline, web_api
from gastrobrain.access import AccessScope
from gastrobrain.meeting_qa import MeetingSearchPlan, format_meeting_record, plan_meeting_search

MEETING_ID = UUID("00000000-0000-0000-0000-000000000001")
START = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def record(**changes):
    data = dict(
        meeting_id=str(MEETING_ID), title="予算会議", summary="広告予算を提案。",
        scheduled_at=START - timedelta(minutes=10), started_at=START,
        ended_at=START + timedelta(minutes=30, seconds=12), status="ended",
        invitees=[("invitee@example.com", True)], speakers=["田中", "佐藤"],
        transcript="田中: 広告予算は50万円です。",
    )
    data.update(changes)
    return format_meeting_record(**data)


def test_duration_uses_recorded_times_not_scheduled_start():
    context = record()
    assert "30分12秒（1812秒）" in context
    assert "40分" not in context
    assert "AI参加時刻" in context
    assert "会議全体の長さと一致するとは限らない" in context


@pytest.mark.parametrize("changes", [
    {"started_at": None}, {"ended_at": None}, {"ended_at": START - timedelta(seconds=1)},
])
def test_missing_or_invalid_times_never_produce_duration(changes):
    assert "記録上の所要時間: 不明" in record(**changes)


def test_timezone_offsets_and_zero_duration():
    assert "0分0秒（0秒）" in record(ended_at=START.astimezone(timezone(timedelta(hours=9))))


def test_empty_records_and_early_speakers_remain_explicit():
    context = record(summary=None, invitees=[], transcript="（前半省略）\n佐藤: はい。")
    assert "- 田中" in context  # Independent of the retained transcript tail.
    assert "招待者（出席確認ではない）:\n記録なし" in context
    assert "完全な出席名簿ではない" in context
    assert "要約:\n記録なし" in context


@pytest.mark.parametrize("response, expected", [
    ('{"external_query": null}', MeetingSearchPlan(None)),
    ('{"external_query": " 広告予算50万円の社内承認規程 "}', MeetingSearchPlan("広告予算50万円の社内承認規程")),
    ('```json\n{"external_query": null}\n```', MeetingSearchPlan(None)),
    ('{}', MeetingSearchPlan(None, True)),
    ('{"external_query": ""}', MeetingSearchPlan(None, True)),
    ('{"external_query": false}', MeetingSearchPlan(None, True)),
    ('not json', MeetingSearchPlan(None, True)),
])
def test_routing_validates_output_and_fails_to_meeting_only(monkeypatch, response, expected):
    monkeypatch.setattr(llm, "complete", lambda **kw: llm.Completion(response))
    assert plan_meeting_search("この提案は会社の規程に沿っている？", [], record()) == expected


def test_routing_resolves_followups_with_meeting_context(monkeypatch):
    complete = MagicMock(return_value=llm.Completion('{"external_query": null}'))
    monkeypatch.setattr(llm, "complete", complete)
    plan_meeting_search("それは？", [{"role": "assistant", "content": "広告予算50万円[9]"}], record())
    payload = complete.call_args.kwargs["messages"][0]["content"]
    assert "広告予算50万円" in payload
    assert "[9]" not in payload
    assert str(MEETING_ID) in payload
    assert "田中: 広告予算は50万円" in payload


def test_routing_failure_does_not_search_literal_question(monkeypatch):
    monkeypatch.setattr(llm, "complete", MagicMock(side_effect=RuntimeError("offline")))
    assert plan_meeting_search("who was in the mtg?", [], record()) == MeetingSearchPlan(None, True)


@pytest.mark.parametrize("plan", [MeetingSearchPlan(None), MeetingSearchPlan(None, True)])
def test_meeting_only_pipeline_never_retrieves_unrelated_documents(monkeypatch, plan):
    monkeypatch.setattr(pipeline, "plan_meeting_search", lambda *args: plan)
    retrieve = MagicMock(side_effect=AssertionError("must not retrieve"))
    monkeypatch.setattr(pipeline, "retrieve_candidates", retrieve)
    captured = {}

    def answer(question, chunks, history, **kwargs):
        captured.update(kwargs)
        assert chunks == []
        yield generate.StreamDone("この会議の記録では30分12秒です。", 0, 0, 0, 0)

    monkeypatch.setattr(pipeline, "answer_stream", answer)

    async def run():
        return [event async for event in pipeline.run_pipeline(pipeline.PipelineInput(
            question="how long was the mtg?", user_id="test", extra_context=record()
        ))]

    events = asyncio.run(run())
    retrieve.assert_not_called()
    assert events[-1].chunks == []
    assert "30分12秒" in events[-1].answer
    assert ("検索要否を判定できず" in captured["extra_context"]) == plan.unavailable


def test_mixed_pipeline_preserves_acl_and_both_evidence_sources(monkeypatch):
    search = "広告予算50万円の社内承認規程"
    monkeypatch.setattr(pipeline, "plan_meeting_search", lambda *args: MeetingSearchPlan(search))
    scope = AccessScope(user_code="allowed", slack_user_id="U123")
    chunk = object()
    retrieve = MagicMock(return_value=[chunk])
    monkeypatch.setattr(pipeline, "retrieve_candidates", retrieve)
    monkeypatch.setattr(pipeline, "rerank_candidates", lambda q, c, s: c)
    monkeypatch.setattr(pipeline, "expand_conversation_parents", lambda c: c)

    def answer(question, chunks, history, **kwargs):
        assert chunks == [chunk]
        assert "広告予算は50万円" in kwargs["extra_context"]
        yield generate.StreamDone("会社の規程では承認が必要です。[1]", 0, 0, 0, 0)

    monkeypatch.setattr(pipeline, "answer_stream", answer)

    async def run():
        return [e async for e in pipeline.run_pipeline(pipeline.PipelineInput(
            question="この提案は会社の規程に沿っている？", user_id="test",
            scope=scope, extra_context=record(),
        ))]

    events = asyncio.run(run())
    assert retrieve.call_args.args[0] == search
    assert retrieve.call_args.args[2] == scope
    assert events[-1].chunks == [chunk]


def test_normal_chat_keeps_existing_retrieval_path(monkeypatch):
    router = MagicMock(side_effect=AssertionError("not a meeting"))
    monkeypatch.setattr(pipeline, "plan_meeting_search", router)
    retrieve = MagicMock(return_value=[])
    monkeypatch.setattr(pipeline, "retrieve_candidates", retrieve)
    monkeypatch.setattr(pipeline, "standalone_query", lambda q, h: "会社の承認規程")

    async def run():
        return [e async for e in pipeline.run_pipeline(pipeline.PipelineInput(
            question="その規程は？", user_id="test", history=[{"role": "user", "content": "承認"}],
        ))]

    asyncio.run(run())
    router.assert_not_called()
    assert retrieve.call_args.args[0] == "会社の承認規程"


def test_meeting_evidence_rules_are_system_instructions_only_for_meeting_answers(monkeypatch):
    stream = MagicMock(return_value=iter([llm.Final("answer")]))
    monkeypatch.setattr(llm, "stream", stream)
    list(generate.answer_stream("誰が参加した？", [], extra_context=record()))
    prompt = stream.call_args.kwargs["system"]
    assert generate._MEETING_RULES in prompt
    assert "他の会議や会社資料で欠けた事実を補完しない" in prompt
    assert generate._MEETING_RULES not in generate.system_prompt("web")


def prep_db(monkeypatch, allowed=True):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchone.side_effect = (
        [(MEETING_ID,), (1,), ("予算会議", None, START, START, START + timedelta(minutes=30), "ended"),
         ("allowed", "U123"), (MEETING_ID,), None]
        if allowed else [(MEETING_ID,), None]
    )
    cur.fetchall.side_effect = [
        [("invitee@example.com", True)], [("Early speaker",), ("Late speaker",)],
        [("Early speaker", "a" * 17000), ("Late speaker", "終わり")], [],
    ]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cur
    monkeypatch.setattr(web_api, "conn", lambda: connection)
    return cur


def prep():
    return web_api._prep_turn(
        conversation_id=MEETING_ID,
        user=MagicMock(user_id=MEETING_ID, email="invitee@example.com"),
        question="who was in the mtg?",
    )


def test_context_queries_stay_scoped_and_exclude_shared_viewers(monkeypatch):
    cur = prep_db(monkeypatch)
    context = prep()[-1]
    assert "- Early speaker" in context
    assert "前半省略" in context
    assert "30分0秒" in context
    calls = cur.execute.call_args_list
    invite_query = next(c for c in calls if "SELECT email, is_organizer" in c.args[0])
    assert "added_by IS NULL" in invite_query.args[0]
    assert invite_query.args[1] == (str(MEETING_ID),)
    segment_queries = [c for c in calls if "FROM meeting_segments" in c.args[0]]
    assert len(segment_queries) == 2
    for query in segment_queries:
        assert "WHERE meeting_id = %s" in query.args[0]
        assert query.args[1] == (str(MEETING_ID),)


def test_revoked_participant_cannot_load_meeting_context(monkeypatch):
    cur = prep_db(monkeypatch, allowed=False)
    with pytest.raises(HTTPException) as error:
        prep()
    assert error.value.status_code == 404
    assert cur.execute.call_count == 2  # Thread ownership, then meeting access.
