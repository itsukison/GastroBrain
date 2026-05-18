from __future__ import annotations

import logging

import anthropic

from gastrobrain.config import settings
from gastrobrain.generate import HistoryTurn, strip_citations

log = logging.getLogger("gastrobrain.rewrite")

_SYSTEM = """あなたは会話履歴を踏まえて、フォローアップ質問を「単独で意味が通る検索クエリ」に書き換える専門家です。

ルール:
1. 出力は1行のみ。書き換えた質問テキストだけを出力する。前置きや解説、引用符は一切付けない。
2. 直前のやり取りで言及された固有名詞・期間・主体・対象を、代名詞（「それ」「あれ」「この」「先ほどの」など）の代わりに明示する。
3. 質問の意味・条件を変えない。新しい条件や推測を加えない。
4. すでに単独で意味が通る場合は、そのまま出力する。
5. 必ず元の言語（日本語の質問は日本語のまま）で出力する。"""

_MAX_HISTORY_TURNS_FOR_REWRITE = 6


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.claude_api_key)


def standalone_query(question: str, history: list[HistoryTurn] | None) -> str:
    """Rewrite a follow-up question into a standalone retrieval query.

    Returns the original question unchanged when history is empty, the rewrite
    call fails, or the model returns an empty string. The failure mode is
    'degrade to literal query', never block the user."""
    if not history:
        return question

    recent: list[HistoryTurn] = []
    for turn in history[-_MAX_HISTORY_TURNS_FOR_REWRITE:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if role == "assistant":
            content = strip_citations(content)
            if not content:
                continue
        recent.append({"role": role, "content": content})

    if not recent:
        return question

    transcript = "\n".join(
        f"{'ユーザー' if t['role'] == 'user' else 'アシスタント'}: {t['content']}"
        for t in recent
    )
    user_msg = (
        "これまでの会話:\n"
        f"{transcript}\n\n"
        f"フォローアップ質問: {question}\n\n"
        "上記の会話を踏まえ、フォローアップ質問を単独で意味が通る検索クエリに書き換えてください。"
    )

    try:
        resp = _client().messages.create(
            model=settings.anthropic_haiku_model,
            max_tokens=256,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        rewritten = "".join(b.text for b in resp.content if b.type == "text").strip()
        rewritten = rewritten.strip("「」\"' \n")
        if not rewritten:
            return question
        return rewritten
    except Exception:
        log.exception("standalone_query rewrite failed; falling back to literal question")
        return question
