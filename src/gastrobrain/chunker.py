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
