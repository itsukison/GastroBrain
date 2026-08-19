"""Unit tests for the chat-LLM provider shim (no network, no DB).

Covers the two things that break silently when the provider is switched: the
cost formula must follow the active provider's rate card, and OpenAI usage must
be normalised onto Anthropic's semantics before it reaches the `queries` table
(`input_tokens` excludes cache reads there).
"""

from dataclasses import dataclass

import pytest

from gastrobrain import llm


@pytest.fixture
def as_provider(monkeypatch):
    def _set(name: str):
        monkeypatch.setattr(llm, "provider", lambda: name)

    return _set


class TestProviderSelection:
    def test_default_is_openai(self):
        assert llm.provider() == "openai"

    def test_api_key_field_follows_provider(self, as_provider):
        as_provider("anthropic")
        assert llm.api_key_field() == "claude_api_key"
        as_provider("openai")
        assert llm.api_key_field() == "openai_api_key"

    def test_mini_and_main_models_differ(self, as_provider):
        for name in ("anthropic", "openai"):
            as_provider(name)
            assert llm._model(mini=True) != llm._model(mini=False)


class TestCost:
    def test_anthropic_rate(self, as_provider):
        as_provider("anthropic")
        # 1M in + 1M out at $3/$15, ¥150/$ → ¥450 + ¥2250
        assert llm.cost_jpy(1_000_000, 1_000_000) == pytest.approx(2700.0)

    def test_openai_is_cheaper_for_the_same_tokens(self, as_provider):
        as_provider("anthropic")
        anthropic_cost = llm.cost_jpy(1000, 500)
        as_provider("openai")
        assert llm.cost_jpy(1000, 500) < anthropic_cost

    def test_unknown_provider_falls_back_to_anthropic_rate(self, as_provider):
        as_provider("anthropic")
        expected = llm.cost_jpy(1000, 500)
        as_provider("bogus")
        assert llm.cost_jpy(1000, 500) == expected


@dataclass
class _Details:
    cached_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class _OpenAIUsage:
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_details: _Details | None = None


class TestOpenAIUsageNormalisation:
    def test_cached_tokens_are_split_out_of_input(self):
        u = llm._openai_usage(
            _OpenAIUsage(1000, 200, _Details(cached_tokens=800, cache_write_tokens=50))
        )
        # OpenAI's prompt_tokens includes cached; Anthropic's input_tokens does not.
        assert u.input_tokens == 200
        assert u.cache_read_input_tokens == 800
        assert u.cache_creation_input_tokens == 50
        assert u.output_tokens == 200

    def test_missing_details_means_no_cache(self):
        u = llm._openai_usage(_OpenAIUsage(120, 30))
        assert (u.input_tokens, u.cache_read_input_tokens) == (120, 0)

    def test_none_usage_is_zeroed(self):
        u = llm._openai_usage(None)
        assert (u.input_tokens, u.output_tokens) == (0, 0)
