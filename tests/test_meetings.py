"""Unit tests for the meetings surface (no network, no DB).

The two invariants worth guarding here are the ones a refactor breaks silently:
the visibility predicate must keep filtering on the participant list, and the
summary parser must never lose the model's text just because it wrapped the
JSON in a fence.
"""

from gastrobrain.web_api import (
    _AGENT_STATES,
    _MEETING_STATUSES,
    _parse_summary,
    meeting_visible_sql,
)


class TestVisibilityPredicate:
    def test_filters_on_participant_list(self):
        sql = meeting_visible_sql()
        assert "meeting_participants" in sql
        assert "p.email = lower(%s)" in sql

    def test_excludes_soft_deleted(self):
        assert "m.deleted_at IS NULL" in meeting_visible_sql()

    def test_takes_exactly_one_parameter(self):
        # Two would silently mis-bind at every call site.
        assert meeting_visible_sql().count("%s") == 1

    def test_alias_is_applied_everywhere(self):
        sql = meeting_visible_sql("mm")
        assert "mm.deleted_at" in sql
        assert "p.meeting_id = mm.id" in sql
        assert " m.deleted_at" not in f" {sql}"

    def test_is_a_conjunction_not_a_disjunction(self):
        # An OR here would make every meeting visible to everyone.
        assert " OR " not in meeting_visible_sql().upper()


class TestSummaryParsing:
    def test_plain_json(self):
        summary, actions = _parse_summary(
            '{"summary": "## 決定事項\\n- やる", "next_actions": '
            '[{"text": "見積もりを出す", "owner": "田中"}]}'
        )
        assert summary.startswith("## 決定事項")
        assert actions == [{"text": "見積もりを出す", "owner": "田中"}]

    def test_fenced_json(self):
        summary, actions = _parse_summary(
            '```json\n{"summary": "要約", "next_actions": []}\n```'
        )
        assert summary == "要約"
        assert actions == []

    def test_non_json_is_kept_as_the_summary(self):
        # Losing the text entirely is worse than losing the structure.
        raw = "要約: 楽天のSKU上限について話した。"
        summary, actions = _parse_summary(raw)
        assert summary == raw
        assert actions == []

    def test_actions_without_text_are_dropped(self):
        _, actions = _parse_summary(
            '{"summary": "x", "next_actions": [{"owner": "田中"}, {"text": "  "}]}'
        )
        assert actions == []

    def test_missing_owner_becomes_empty_string(self):
        _, actions = _parse_summary('{"summary": "x", "next_actions": [{"text": "やる"}]}')
        assert actions == [{"text": "やる", "owner": ""}]


class TestStateVocabulary:
    def test_agent_states_match_the_contract(self):
        # §6.4: two values, and the 90 s expiry lives on the meeting side.
        assert {"asleep", "open"} == _AGENT_STATES

    def test_statuses_match_the_schema_check_constraint(self):
        assert {"scheduled", "joining", "live", "ended", "failed"} == _MEETING_STATUSES
