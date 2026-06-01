import re
from dataclasses import dataclass

from markdown_it import MarkdownIt

from gastrobrain.config import settings


@dataclass
class Chunk:
    ordinal: int
    heading_path: list[str]
    content: str

    @property
    def char_count(self) -> int:
        return len(self.content)


def chunk_markdown(text: str) -> list[Chunk]:
    """Structure-aware chunker.

    Walks the markdown token stream, tracks heading_path, and greedy-packs
    paragraph-level blocks under the same heading into chunks bounded by
    `chunk_target_chars`. Headings are themselves prepended to chunks as
    soft context (helps embeddings + rerank disambiguate similar bodies).
    """
    md = MarkdownIt("commonmark", {"html": False})
    tokens = md.parse(text)

    heading_stack: list[str] = []
    current_heading_level = 0
    blocks: list[tuple[list[str], str]] = []
    pending_inline: str | None = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "heading_open":
            level = int(tok.tag[1])
            inline = tokens[i + 1]
            title = inline.content.strip()
            while len(heading_stack) >= level:
                heading_stack.pop()
            heading_stack.append(title)
            current_heading_level = level
            i += 3
            continue

        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            pending_inline = inline.content.strip()
            i += 3
            if pending_inline:
                blocks.append((list(heading_stack), pending_inline))
            pending_inline = None
            continue

        if tok.type in ("bullet_list_open", "ordered_list_open"):
            list_text, consumed = _render_list(tokens, i)
            if list_text:
                blocks.append((list(heading_stack), list_text))
            i += consumed
            continue

        if tok.type == "fence":
            blocks.append((list(heading_stack), f"```{tok.info}\n{tok.content.rstrip()}\n```"))
            i += 1
            continue

        if tok.type == "code_block":
            blocks.append((list(heading_stack), tok.content.rstrip()))
            i += 1
            continue

        if tok.type == "table_open":
            table_text, consumed = _render_table(tokens, i)
            if table_text:
                blocks.append((list(heading_stack), table_text))
            i += consumed
            continue

        i += 1

    return _pack(blocks)


def _render_list(tokens, start: int) -> tuple[str, int]:
    end = _find_close(tokens, start, "bullet_list_close", "ordered_list_close")
    items: list[str] = []
    j = start + 1
    while j < end:
        if tokens[j].type == "list_item_open":
            close = _find_close(tokens, j, "list_item_close")
            text_parts: list[str] = []
            for k in range(j + 1, close):
                if tokens[k].type == "inline":
                    text_parts.append(tokens[k].content.strip())
            if text_parts:
                items.append("- " + " ".join(text_parts))
            j = close + 1
        else:
            j += 1
    return "\n".join(items), end - start + 1


def _render_table(tokens, start: int) -> tuple[str, int]:
    end = _find_close(tokens, start, "table_close")
    rows: list[list[str]] = []
    current_row: list[str] = []
    in_cell = False
    for j in range(start + 1, end):
        t = tokens[j]
        if t.type in ("td_open", "th_open"):
            in_cell = True
            current_row.append("")
        elif t.type in ("td_close", "th_close"):
            in_cell = False
        elif in_cell and t.type == "inline":
            current_row[-1] = t.content.strip()
        elif t.type == "tr_close":
            rows.append(current_row)
            current_row = []
    return "\n".join(" | ".join(r) for r in rows), end - start + 1


def _find_close(tokens, start: int, *close_types: str) -> int:
    open_type = tokens[start].type
    base_open = open_type.replace("_open", "")
    depth = 0
    for k in range(start, len(tokens)):
        if tokens[k].type == open_type:
            depth += 1
        elif tokens[k].type in close_types or tokens[k].type == f"{base_open}_close":
            depth -= 1
            if depth == 0:
                return k
    return len(tokens) - 1


def _pack(blocks: list[tuple[list[str], str]]) -> list[Chunk]:
    target = settings.chunk_target_chars
    overlap = settings.chunk_overlap_chars
    out: list[Chunk] = []
    buf: list[str] = []
    buf_heading: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        body = "\n\n".join(buf)
        prefix = " > ".join(buf_heading) if buf_heading else ""
        content = f"[{prefix}]\n{body}" if prefix else body
        out.append(Chunk(ordinal=len(out), heading_path=list(buf_heading), content=content))
        buf = []
        buf_len = 0

    for heading, body in blocks:
        if heading != buf_heading and buf:
            flush()
        buf_heading = heading
        if buf_len + len(body) > target and buf:
            flush()
        if len(body) > target:
            for piece in _split_long(body, target, overlap):
                buf.append(piece)
                flush()
            continue
        buf.append(body)
        buf_len += len(body)
    flush()
    return out


def _split_long(text: str, target: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + target, len(text))
        pieces.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return pieces


# A line like "緒方若菜 00:00" or "ハイタッチまつもと 17:16" — speaker name,
# whitespace, then mm:ss (optionally hh:mm:ss). Auto-generated Meet/Gemini
# transcripts use this to delimit turns; the summary/audit section above the
# first such line has none (its colons are full-width or in "22/60"-style scores).
_SPEAKER_RE = re.compile(r"^.{1,40}?\s+\d{1,2}:\d{2}(?::\d{2})?$")


def chunk_transcript(text: str) -> list[Chunk]:
    """Chunker for auto-generated meeting transcripts (no markdown headings).

    Each file is `<summary + AI audit report>` followed by a speaker-by-speaker
    transcript. We split at the first speaker line, then pack each section
    independently: the head by paragraph, the transcript by speaker turn. Each
    chunk is tagged with its section (要約・監査 / 文字起こし) so retrieval and
    citations carry that context, mirroring chunk_markdown's heading prefix.
    """
    head, transcript = _split_head_transcript(text)
    target = settings.chunk_target_chars
    overlap = settings.chunk_overlap_chars

    out: list[Chunk] = []
    sections = [
        ("要約・監査", _paragraphs(head)),
        ("文字起こし", _speaker_turns(transcript)),
    ]
    for label, units in sections:
        for piece in _pack_units(units, target, overlap):
            out.append(
                Chunk(ordinal=len(out), heading_path=[label], content=f"[{label}]\n{piece}")
            )
    return out


def _split_head_transcript(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if _SPEAKER_RE.match(line.strip()):
            return "\n".join(lines[:idx]).strip(), "\n".join(lines[idx:]).strip()
    return text.strip(), ""


def _paragraphs(text: str) -> list[str]:
    if not text:
        return []
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _speaker_turns(text: str) -> list[str]:
    if not text:
        return []
    turns: list[str] = []
    cur: list[str] = []
    for line in text.splitlines():
        if _SPEAKER_RE.match(line.strip()) and cur:
            turns.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        turns.append("\n".join(cur))
    return [t for t in turns if t.strip()]


def _pack_units(units: list[str], target: int, overlap: int) -> list[str]:
    """Greedy-pack units to ~target chars; window any single oversize unit.
    Same shape as _pack(), but for plain (non-markdown) unit lists."""
    out: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            out.append("\n\n".join(buf))
            buf = []
            buf_len = 0

    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        if len(unit) > target:
            flush()
            out.extend(_split_long(unit, target, overlap))
            continue
        if buf_len + len(unit) > target and buf:
            flush()
        buf.append(unit)
        buf_len += len(unit)
    flush()
    return out
