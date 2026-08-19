"""Chat-LLM provider shim — Anthropic (Claude) or OpenAI, chosen by LLM_PROVIDER.

Every chat call in the app goes through here: answer generation (generate.py),
follow-up query rewriting (rewrite.py) and thread titles (web_api.py). Retrieval
(Cohere embed/rerank) and the voice agent are separate and unaffected.

Usage is normalised onto Anthropic's semantics so the `queries` table keeps one
meaning per column: `input_tokens` EXCLUDES cache reads, `cache_read_input_tokens`
counts them. OpenAI's `prompt_tokens` includes cached tokens, so it is adjusted.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from gastrobrain.config import settings

# ¥ per 1M tokens (input, output) for the *main* model of each provider.
# USD x 150. anthropic: claude-sonnet-4-6 $3/$15. openai: gpt-5.6-terra $2/$12.
_RATES_JPY: dict[str, tuple[float, float]] = {
    "anthropic": (3 * 150, 15 * 150),
    "openai": (2 * 150, 12 * 150),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class Delta:
    """A streamed text fragment."""

    text: str


@dataclass
class Final:
    """End of a stream: the full text plus usage."""

    text: str
    usage: Usage = field(default_factory=Usage)


@dataclass
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)


def provider() -> str:
    return settings.llm_provider.strip().lower()


def api_key_field() -> str:
    """The settings field that must be set for the active provider."""
    return "openai_api_key" if provider() == "openai" else "claude_api_key"


def cost_jpy(input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = _RATES_JPY.get(provider(), _RATES_JPY["anthropic"])
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


# --------------------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------------------

_anthropic_client = None
_openai_client = None


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        _anthropic_client = anthropic.Anthropic(api_key=settings.claude_api_key)
    return _anthropic_client


def _openai():
    global _openai_client
    if _openai_client is None:
        import openai

        _openai_client = openai.OpenAI(api_key=settings.openai_api_key)
    return _openai_client


def _model(mini: bool) -> str:
    if provider() == "openai":
        return settings.openai_mini_model if mini else settings.openai_model
    return settings.anthropic_haiku_model if mini else settings.anthropic_model


def _openai_usage(usage) -> Usage:
    if usage is None:
        return Usage()
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    written = getattr(details, "cache_write_tokens", 0) or 0
    prompt = usage.prompt_tokens or 0
    return Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=usage.completion_tokens or 0,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=written,
    )


# --------------------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------------------


def complete(
    *,
    system: str,
    messages: list[dict],
    max_tokens: int,
    mini: bool = False,
) -> Completion:
    if provider() == "openai":
        resp = _openai().chat.completions.create(
            model=_model(mini),
            max_completion_tokens=max_tokens,
            reasoning_effort=settings.openai_reasoning_effort,
            messages=[{"role": "system", "content": system}, *messages],
        )
        return Completion(
            text=resp.choices[0].message.content or "",
            usage=_openai_usage(resp.usage),
        )

    resp = _anthropic().messages.create(
        model=_model(mini),
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
    u = resp.usage
    return Completion(
        text="".join(b.text for b in resp.content if b.type == "text"),
        usage=Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        ),
    )


def stream(
    *,
    system: str,
    messages: list[dict],
    max_tokens: int,
    mini: bool = False,
) -> Iterator[Delta | Final]:
    """Yields Delta(text) per fragment, then exactly one Final(text, usage)."""
    buf: list[str] = []

    if provider() == "openai":
        chunks = _openai().chat.completions.create(
            model=_model(mini),
            max_completion_tokens=max_tokens,
            reasoning_effort=settings.openai_reasoning_effort,
            messages=[{"role": "system", "content": system}, *messages],
            stream=True,
            stream_options={"include_usage": True},
        )
        usage = Usage()
        for chunk in chunks:
            if chunk.usage is not None:
                usage = _openai_usage(chunk.usage)
            for choice in chunk.choices:
                text = choice.delta.content
                if text:
                    buf.append(text)
                    yield Delta(text=text)
        yield Final(text="".join(buf), usage=usage)
        return

    with _anthropic().messages.stream(
        model=_model(mini),
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    ) as s:
        for text in s.text_stream:
            if text:
                buf.append(text)
                yield Delta(text=text)
        u = s.get_final_message().usage
        yield Final(
            text="".join(buf),
            usage=Usage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            ),
        )
