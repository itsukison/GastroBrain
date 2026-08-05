"""Chatwork ingestion CLI — room messages grouped into per-day conversation docs.

Mirrors the Slack path (slack_ingest.py): Gastroduce conversations are bursty and
low-volume, so the natural unit is a *day*, not a thread. One day-window of a room
→ one document; messages render chronologically speaker-by-speaker so the
transcript chunker packs them turn-by-turn. `meeting_date` is the window start, so
date-scoped questions filter straight to that day, and retrieval expands a matched
chunk back to its full day at answer time (retrieve.expand_conversation_parents).

Two hard limits of the Chatwork API shape this design (see docs research):
  - **Forward-only.** GET /rooms/{id}/messages returns at most ~100 messages and
    has no history pagination (only `force=1` for the latest 100). We capture
    conversations from now on, not the existing archive.
  - **No per-user ACL.** The members endpoint exposes no email, and Chatwork has
    no public-room concept, so docs can't be gated per user like Slack/NotePM.
    Only an operator-curated room allowlist (CHATWORK_ROOM_IDS) is ingested, and
    those docs are visible to every signed-in user (unrestricted, like gdrive).

Auth: a single Chatwork API token (settings.chatwork_api_token / CHATWORK_API),
sent as the `x-chatworktoken` header. It sees only rooms its account belongs to.

Entry points:
  gb-chatwork-dryrun  — resolve allowlisted rooms, group by day, count + cost.
  gb-chatwork-ingest  — fetch, group by day, chunk, embed, write source='chatwork'.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
import psycopg
import typer
from rich.console import Console
from rich.table import Table
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from gastrobrain.chunker import chunk_transcript
from gastrobrain.config import settings
from gastrobrain.db import conn
from gastrobrain.embed import embed_texts
from gastrobrain.notepm_cli import (
    _DB_RETRY,
    COHERE_USD_PER_1M,
    _estimate_tokens,
    _TokenRateLimiter,
)

console = Console()

CHATWORK_SOURCE = "chatwork"
API_BASE = "https://api.chatwork.com/v2"
JST = timezone(timedelta(hours=9))
# Day-windows whose real text (mentions/markup stripped) is shorter than this
# are dropped as noise — near-empty days of bare pings or one-liner stamps.
MIN_CONTENT_CHARS = 15


def _is_transient(exc: BaseException) -> bool:
    """Retry network errors and Chatwork 429/5xx. A 4xx other than 429 (bad token,
    not a member of the room) is permanent and propagates immediately."""
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


_CW_RETRY = retry(
    stop=stop_after_attempt(8),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception(_is_transient),
    reraise=True,
)


@dataclass
class DayDoc:
    key: date  # window start (JST)
    messages: list[dict] = field(default_factory=list)  # chronological

    @property
    def latest_time(self) -> int:
        return max(int(m["send_time"]) for m in self.messages)

    @property
    def first_id(self) -> str:
        return min(self.messages, key=lambda m: int(m["send_time"]))["message_id"]


class ChatworkClient:
    def __init__(self) -> None:
        token = settings.chatwork_api_token
        if not token:
            raise RuntimeError("CHATWORK_API is not set (see config.py / .env).")
        self._c = httpx.Client(
            base_url=API_BASE,
            headers={"x-chatworktoken": token},
            timeout=30.0,
        )

    @_CW_RETRY
    def _get(self, path: str, **params) -> httpx.Response:
        r = self._c.get(path, params=params or None)
        r.raise_for_status()
        return r

    def me(self) -> dict:
        return self._get("/me").json()

    def list_rooms(self) -> list[dict]:
        """Every room the token's account belongs to. Each has room_id, name,
        type (my/direct/group), role, message_num, last_update_time, …"""
        return self._get("/rooms").json()

    def get_messages(self, room_id: str) -> list[dict]:
        """Latest ~100 messages of a room. `force=1` always returns them from the
        top (ignoring the read cursor). A 204 (no messages) yields an empty body."""
        r = self._get(f"/rooms/{room_id}/messages", force=1)
        if r.status_code == 204 or not r.content:
            return []
        return r.json()


# ── Body cleanup ──────────────────────────────────────────────────────────
# Chatwork message bodies carry structural tags (Message Notation). Resolve
# mention/reply tags to @name when the account is known, drop the rest, and
# unescape entities — leaving plain readable transcript text.
_TO_RE = re.compile(r"\[(?:To|rp aid)=?:?\s*(\d+)[^\]]*\]")
_PICON_RE = re.compile(r"\[/?picon(?:name)?:\d+\]")
_TAG_RE = re.compile(
    r"\[/?(?:qt|qtmeta|info|title|code|hr|dtext|download|preview|deleted|task|limit)[^\]]*\]",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"[ \t]*\n[ \t]*\n\s*")
_MENTION_TOKEN_RE = re.compile(r"@\S+")


def _clean(body: str, names: dict[str, str]) -> str:
    def to_repl(m: re.Match) -> str:
        return "@" + names.get(m.group(1), "") + " "

    text = _TO_RE.sub(to_repl, body or "")
    text = _PICON_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return _WS_RE.sub("\n\n", text).strip()


def _name_map(messages: list[dict]) -> dict[str, str]:
    """account_id → display name, harvested from the messages themselves (the
    message object carries account.name, so no separate members call is needed)."""
    out: dict[str, str] = {}
    for m in messages:
        acc = m.get("account") or {}
        if acc.get("account_id") is not None:
            out[str(acc["account_id"])] = acc.get("name") or str(acc["account_id"])
    return out


def _bucket_key(send_time: int) -> date:
    return datetime.fromtimestamp(int(send_time), JST).date()


def _bucket(messages: list[dict]) -> list[DayDoc]:
    buckets: dict[date, list[dict]] = defaultdict(list)
    for m in messages:
        buckets[_bucket_key(m["send_time"])].append(m)
    docs = [
        DayDoc(key=k, messages=sorted(v, key=lambda m: int(m["send_time"])))
        for k, v in buckets.items()
    ]
    docs.sort(key=lambda d: d.key, reverse=True)  # newest-first
    return docs


def _content_len(doc: DayDoc, names: dict[str, str]) -> int:
    joined = " ".join(_clean(m.get("body", ""), names) for m in doc.messages)
    return len("".join(_MENTION_TOKEN_RE.sub("", joined).split()))


def _is_substantive(doc: DayDoc, names: dict[str, str]) -> bool:
    return _content_len(doc, names) >= MIN_CONTENT_CHARS


def _render(doc: DayDoc, names: dict[str, str]) -> str:
    """Speaker-per-turn so chunk_transcript's speaker regex ("<name> HH:MM")
    fires on each message and packs the day turn-by-turn."""
    lines: list[str] = []
    for m in doc.messages:
        acc = m.get("account") or {}
        name = acc.get("name") or names.get(str(acc.get("account_id")), "unknown")
        hhmm = datetime.fromtimestamp(int(m["send_time"]), JST).strftime("%H:%M")
        lines.append(f"{name} {hhmm}\n{_clean(m.get('body', ''), names)}")
    return "\n\n".join(lines)


def _title(room_name: str, doc: DayDoc) -> str:
    return f"💬{room_name}・{doc.key.isoformat()}"


def _permalink(room_id: str, message_id: str) -> str:
    return f"https://www.chatwork.com/#!rid{room_id}-{message_id}"


def _targets(client: ChatworkClient, rooms_opt: str) -> list[tuple[str, str]]:
    """(room_id, name) for each allowlisted room. The allowlist comes from
    --rooms or, if empty, settings.chatwork_room_ids. Names are resolved from the
    room list; an id absent there (token not a member) is reported and skipped."""
    raw = rooms_opt or settings.chatwork_room_ids
    ids = [r.strip() for r in raw.split(",") if r.strip()]
    if not ids:
        raise typer.BadParameter(
            "No rooms. Set CHATWORK_ROOM_IDS or pass --rooms 123,456 (comma-separated room_ids)."
        )
    name_by_id = {str(r["room_id"]): r.get("name", "") for r in client.list_rooms()}
    out: list[tuple[str, str]] = []
    for rid in ids:
        if rid not in name_by_id:
            console.print(f"  [yellow]skip room {rid}: token's account is not a member[/yellow]")
            continue
        out.append((rid, name_by_id[rid]))
    return out


def dryrun(
    rooms: str = typer.Option("", help="Comma-separated room_ids. Defaults to CHATWORK_ROOM_IDS."),
) -> None:
    """Resolve allowlisted rooms, group their latest messages by day, estimate
    embed cost. No embedding, no DB writes."""
    client = ChatworkClient()
    targets = _targets(client, rooms)
    console.print(f"[bold]Chatwork dryrun:[/bold] {len(targets)} room(s)")

    grand_docs = grand_msgs = grand_tokens = 0
    t = Table("room", "day-docs", "msgs", "~tokens", "~$", title="Per-room projection")
    for rid, rname in targets:
        messages = client.get_messages(rid)
        names = _name_map(messages)
        docs = [d for d in _bucket(messages) if _is_substantive(d, names)]
        if not docs:
            continue
        tokens = sum(_estimate_tokens(_render(d, names)) for d in docs)
        n_msgs = sum(len(d.messages) for d in docs)
        grand_docs += len(docs)
        grand_msgs += n_msgs
        grand_tokens += tokens
        t.add_row(rname, str(len(docs)), str(n_msgs),
                  f"{tokens:,}", f"{tokens * COHERE_USD_PER_1M / 1_000_000:.2f}")

    console.print(t)
    console.print(
        f"\n[bold]Total:[/bold] {grand_docs} day-docs, {grand_msgs} messages, "
        f"~{grand_tokens:,} embed tokens (~${grand_tokens * COHERE_USD_PER_1M / 1_000_000:.2f}). "
        f"[dim]Forward-only: latest ~100 msgs/room. Rough; the ingest run reports actuals.[/dim]"
    )


@dataclass
class _RunState:
    """Shared budget/counters across the run, so the token budget and --limit are
    global caps across all rooms, not per-room."""
    token_budget: int
    limit: int
    batch_size: int
    rate_limiter: _TokenRateLimiter = field(default_factory=_TokenRateLimiter)
    running_tokens: int = 0
    ingested: int = 0
    chunks_total: int = 0
    skipped_unchanged: int = 0
    skipped_empty: int = 0
    skipped_noise: int = 0
    stop: bool = False
    stop_reason: str = "exhausted"


def _ingest_room(client: ChatworkClient, room_id: str, room_name: str, st: _RunState) -> None:
    folder_path = ["Chatwork", room_name]
    messages = client.get_messages(room_id)
    names = _name_map(messages)
    all_docs = _bucket(messages)
    docs = [d for d in all_docs if _is_substantive(d, names)]
    st.skipped_noise += len(all_docs) - len(docs)
    console.print(
        f"[bold]{room_name}[/bold]: {len(messages)} msgs → {len(all_docs)} day-docs "
        f"({len(docs)} substantive)."
    )

    @_DB_RETRY
    def _persist_with_retry(external_id, title, url, body, updated_at, meeting_date, chs, titled, embeddings, content_hash):
        with conn() as c, c.cursor() as cur:
            n = _persist(cur, external_id, title, url, folder_path, body, updated_at,
                         meeting_date, chs, titled, embeddings, content_hash)
            c.commit()
            return n

    for d in docs:
        body = _render(d, names)
        chs = chunk_transcript(body)
        if not chs:
            st.skipped_empty += 1
            continue

        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        external_id = f"{room_id}:{d.key.isoformat()}"
        title = _title(room_name, d)
        titled = [f"タイトル: {title}\n\n{ch.content}" for ch in chs]

        if _hash_exists(external_id, content_hash):
            st.skipped_unchanged += 1
            continue

        est = sum(_estimate_tokens(t) for t in titled)
        if st.running_tokens + est > st.token_budget:
            st.stop = True
            st.stop_reason = f"token budget would be exceeded ({st.running_tokens + est:,} > {st.token_budget:,})"
            return

        embeddings: list[list[float]] = []
        for i in range(0, len(titled), st.batch_size):
            batch = titled[i:i + st.batch_size]
            slept = st.rate_limiter.reserve(sum(_estimate_tokens(t) for t in batch))
            if slept > 0:
                console.print(f"  [dim]rate-limit pause: {slept:.1f}s[/dim]")
            embeddings.extend(embed_texts(batch, input_type="search_document"))
        st.running_tokens += est

        url = _permalink(room_id, d.first_id)
        updated_at = datetime.fromtimestamp(d.latest_time, timezone.utc)
        try:
            n = _persist_with_retry(external_id, title, url, body, updated_at, d.key,
                                    chs, titled, embeddings, content_hash)
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            console.print(f"  [red]persist failed for {title[:40]}: {type(e).__name__}[/red]")
            continue
        st.ingested += 1
        st.chunks_total += n

        if st.limit and st.ingested >= st.limit:
            st.stop = True
            st.stop_reason = f"hit --limit {st.limit}"
            return


def ingest(
    rooms: str = typer.Option("", help="Comma-separated room_ids. Defaults to CHATWORK_ROOM_IDS."),
    token_budget: int = typer.Option(
        25_000_000,
        help="Hard stop if running token total would exceed this. 25M ≈ $2.50 at $0.10/1M.",
    ),
    limit: int = typer.Option(0, help="Stop after ingesting this many day-docs total (0 = no cap)."),
    batch_size: int = typer.Option(96, help="Cohere embed batch size (max 96)"),
) -> None:
    """Ingest allowlisted Chatwork rooms into Supabase as source='chatwork', one
    document per day-window. Idempotent: a day already present (by content hash)
    is skipped; a day that gained messages re-ingests. Forward-only — only the
    latest ~100 messages per room are reachable via the API."""
    budget_usd = token_budget * COHERE_USD_PER_1M / 1_000_000
    client = ChatworkClient()
    targets = _targets(client, rooms)
    console.print(
        f"[bold]Chatwork ingest:[/bold] {len(targets)} room(s)   "
        f"[bold]Budget:[/bold] {token_budget:,} tokens (~${budget_usd:.2f})"
    )

    st = _RunState(token_budget=token_budget, limit=limit, batch_size=batch_size)
    for rid, rname in targets:
        if st.stop:
            break
        _ingest_room(client, rid, rname, st)

    spent = st.running_tokens * COHERE_USD_PER_1M / 1_000_000
    console.print(f"\n[bold]Done.[/bold] Stop: {st.stop_reason}")
    console.print(f"  Ingested:                [green]{st.ingested}[/green] day-docs, [green]{st.chunks_total}[/green] chunks")
    console.print(f"  Skipped (already in DB): {st.skipped_unchanged}")
    console.print(f"  Skipped (empty):         {st.skipped_empty}")
    console.print(f"  Dropped as noise:        {st.skipped_noise}")
    console.print(f"  [bold]Estimated cost:[/bold] {st.running_tokens:,} tokens (~${spent:.4f})")


@_DB_RETRY
def _hash_exists(external_id: str, content_hash: str) -> bool:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM documents WHERE source = %s AND external_id = %s AND content_hash = %s LIMIT 1",
            (CHATWORK_SOURCE, external_id, content_hash),
        )
        return cur.fetchone() is not None


def _persist(cur, external_id, title, url, folder_path, body, updated_at, meeting_date,
             chunks, titled, embeddings, content_hash) -> int:
    cur.execute(
        "SELECT id FROM documents WHERE source = %s AND external_id = %s",
        (CHATWORK_SOURCE, external_id),
    )
    existing = cur.fetchone()
    if existing:
        doc_id = existing[0]
        cur.execute(
            """UPDATE documents
               SET title=%s, url=%s, author=%s, folder_path=%s,
                   updated_at=%s, raw_markdown=%s, content_hash=%s,
                   meeting_date=%s, deleted_at=NULL
               WHERE id=%s""",
            (title, url, None, folder_path, updated_at, body, content_hash,
             meeting_date, doc_id),
        )
        cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
    else:
        cur.execute(
            """INSERT INTO documents
                  (source, external_id, title, folder_path, url, author,
                   updated_at, raw_markdown, content_hash, meeting_date)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (CHATWORK_SOURCE, external_id, title, folder_path, url, None,
             updated_at, body, content_hash, meeting_date),
        )
        doc_id = cur.fetchone()[0]
    for ch, t, emb in zip(chunks, titled, embeddings, strict=True):
        cur.execute(
            """INSERT INTO chunks
                  (doc_id, ordinal, kind, heading_path, content, token_count, embedding)
               VALUES (%s, %s, 'page', %s, %s, %s, %s)""",
            (doc_id, ch.ordinal, ch.heading_path, t, max(1, len(t) // 2), emb),
        )
    return len(chunks)


def dryrun_cli() -> None:
    typer.run(dryrun)


def ingest_cli() -> None:
    typer.run(ingest)


if __name__ == "__main__":
    dryrun_cli()
