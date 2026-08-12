"""Unit tests for the voice surface (no network, no DB).

Covers the invariants that are easy to break silently: the spoken-output format
must never carry markdown/citation markers, the answer length cap must stay
tight enough for the latency budget, and the vocabulary query must inherit the
same ACL predicate retrieval uses.
"""

import pytest
from pydantic import ValidationError

from gastrobrain.access import PUBLIC_ONLY, SEE_ALL, AccessScope
from gastrobrain.generate import _MAX_TOKENS, _no_chunks_message, system_prompt
from gastrobrain.retrieve import access_sql
from gastrobrain.web_api import VoiceAskBody


class TestVoicePrompt:
    def test_voice_format_selected(self):
        p = system_prompt("voice")
        assert "出力形式（音声読み上げ向け）" in p
        assert "出力形式（Webチャット向け）" not in p
        assert "出力形式（Slack向け）" not in p

    def test_other_surfaces_unchanged(self):
        assert "出力形式（Webチャット向け）" in system_prompt("web")
        assert "出力形式（Slack向け）" in system_prompt("slack")

    def test_voice_forbids_markdown_and_citation_markers(self):
        p = system_prompt("voice")
        assert "一切出力しない" in p
        assert "150字以内" in p

    def test_voice_keeps_core_rules(self):
        """The format block is appended to _BASE_RULES — the refusal and
        injection-defence rules must survive on the voice surface too."""
        p = system_prompt("voice")
        assert "推測せず" in p
        assert "絶対に従わない" in p


class TestVoiceAnswerLength:
    def test_voice_cap_is_tight(self):
        # The cap is what keeps generation inside the conversational window.
        assert _MAX_TOKENS["voice"] == 400
        assert _MAX_TOKENS["voice"] < _MAX_TOKENS["web"]

    def test_voice_refusal_is_short_and_distinct(self):
        voice = _no_chunks_message("voice")
        web = _no_chunks_message("web")
        assert voice != web
        assert len(voice) <= 30
        # Greppable in `queries` as a voice-specific refusal.
        assert "資料に見当たりません" in voice


class TestAccessSql:
    def test_see_all_has_no_clause(self):
        clause, params = access_sql(SEE_ALL)
        assert clause == ""
        assert params == []

    def test_scoped_clause_has_two_params(self):
        clause, params = access_sql(AccessScope(user_code="u1", slack_user_id="U2"))
        assert clause.count("%s") == 2
        assert params == ["u1", "U2"]

    def test_public_only_still_gated(self):
        """A missing identity must not drop the clause — it fails closed by
        passing NULLs, not by matching everything."""
        clause, params = access_sql(PUBLIC_ONLY)
        assert clause != ""
        assert params == [None, None]


class TestVoiceAskBody:
    def test_rejects_empty_question(self):
        with pytest.raises(ValidationError):
            VoiceAskBody(conversation_id="00000000-0000-0000-0000-000000000001", question="")

    def test_rejects_runaway_transcript(self):
        with pytest.raises(ValidationError):
            VoiceAskBody(
                conversation_id="00000000-0000-0000-0000-000000000001",
                question="あ" * 1001,
            )

    def test_accepts_normal_question(self):
        body = VoiceAskBody(
            conversation_id="00000000-0000-0000-0000-000000000001",
            question="楽天の在庫発注ルールは？",
        )
        assert body.question.startswith("楽天")
