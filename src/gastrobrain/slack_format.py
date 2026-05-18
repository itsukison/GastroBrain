"""Convert standard Markdown (what Claude emits) to Slack mrkdwn (what Slack renders).

Differences that matter for our LLM output:
  Markdown          Slack mrkdwn
  --------          ------------
  **bold**          *bold*
  ### Heading       *Heading*           (Slack has no headings)
  [text](url)       <url|text>
  - item            - item              (works as-is)
  > quote           > quote              (works as-is)
  `code`            `code`               (works as-is)
  ```block```       ```block```          (works as-is)

Also splits long bodies into Slack-safe section blocks (3000-char limit per block).
"""

from __future__ import annotations

import re
from typing import Any

SECTION_CHAR_LIMIT = 2900  # 3000 with safety margin


def assign_source_numbers(chunks: list[Any]) -> list[int]:
    """Map each chunk to a 1-based source number, deduplicated by doc_id.

    The first chunk of each unique document gets the next available number;
    subsequent chunks from the same document share that number. Used to keep
    inline citations (`[N]`) in the answer aligned with the rendered sources
    section.
    """
    doc_to_num: dict = {}
    out: list[int] = []
    for c in chunks:
        if c.doc_id not in doc_to_num:
            doc_to_num[c.doc_id] = len(doc_to_num) + 1
        out.append(doc_to_num[c.doc_id])
    return out


def to_slack_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn syntax."""
    if not text:
        return ""

    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _stash_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    def _stash_inline(m: re.Match) -> str:
        inline_codes.append(m.group(0))
        return f"\x01IC{len(inline_codes) - 1}\x01"

    text = re.sub(r"```[\s\S]*?```", _stash_code_block, text)
    text = re.sub(r"`[^`\n]+`", _stash_inline, text)

    # Bold: **x** / __x__ → *x*. Wrap with U+200B on both sides so Slack sees a
    # word boundary even when adjacent to non-ASCII (Japanese) characters —
    # otherwise `*18歳以上*から` renders literally instead of as bold.
    text = re.sub(r"\*\*([^*\n]+)\*\*", "​*\\1*​", text)
    text = re.sub(r"__([^_\n]+)__", "​*\\1*​", text)

    text = re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"*\1*", text, flags=re.MULTILINE)

    text = re.sub(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)", r"<\2|\1>", text)

    text = re.sub(r"^([ \t]*)\*[ \t]+", r"\1• ", text, flags=re.MULTILINE)

    for i, c in enumerate(inline_codes):
        text = text.replace(f"\x01IC{i}\x01", c)
    for i, c in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", c)

    return text


def split_to_section_blocks(text: str, *, char_limit: int = SECTION_CHAR_LIMIT) -> list[dict]:
    """Split a (possibly long) mrkdwn body into Slack section blocks.

    Splits on paragraph boundaries first, then on line boundaries, then hard-cuts.
    Never splits inside a fenced code block.
    """
    if len(text) <= char_limit:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    pieces: list[str] = []
    buf: list[str] = []
    buf_len = 0

    paragraphs = _split_preserving_code(text)
    for para in paragraphs:
        if buf_len + len(para) + 2 <= char_limit:
            buf.append(para)
            buf_len += len(para) + 2
            continue
        if buf:
            pieces.append("\n\n".join(buf))
            buf = []
            buf_len = 0
        if len(para) <= char_limit:
            buf.append(para)
            buf_len = len(para)
        else:
            for chunk in _hard_split(para, char_limit):
                pieces.append(chunk)
    if buf:
        pieces.append("\n\n".join(buf))

    return [{"type": "section", "text": {"type": "mrkdwn", "text": p}} for p in pieces]


def _split_preserving_code(text: str) -> list[str]:
    """Split text on blank lines, but treat fenced code blocks as atomic."""
    out: list[str] = []
    i = 0
    lines = text.split("\n")
    paragraph: list[str] = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
            paragraph.append(line)
            continue
        if not in_code and line.strip() == "":
            if paragraph:
                out.append("\n".join(paragraph))
                paragraph = []
        else:
            paragraph.append(line)
    if paragraph:
        out.append("\n".join(paragraph))
    return out


def _hard_split(text: str, limit: int) -> list[str]:
    """Split text on line boundaries; if a single line exceeds the limit, hard-cut."""
    out: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in text.split("\n"):
        if len(line) > limit:
            if buf:
                out.append("\n".join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(line), limit):
                out.append(line[i : i + limit])
            continue
        if buf_len + len(line) + 1 > limit:
            out.append("\n".join(buf))
            buf, buf_len = [line], len(line)
        else:
            buf.append(line)
            buf_len += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out
