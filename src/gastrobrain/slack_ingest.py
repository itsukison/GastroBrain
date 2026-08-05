"""Slack ingestion CLI — channel messages grouped into per-day conversation docs.

Gastroduce discourages threads (people @-mention and reply as separate top-level
messages), and daily volume is low, so the natural conversation unit is a *day*,
not a thread. One day-window of a channel → one document; messages are rendered
chronologically speaker-by-speaker so the transcript chunker packs them by turn.
`meeting_date` is set to the window's start, so date-scoped questions ("5/14 に
何を話した") filter straight to that day. Retrieval expands a matched Slack chunk
back to its full day at answer time (see pipeline._expand_slack_parents), so the
LLM sees the whole conversation, not a 500-char fragment.

Reuses the existing Gastrobrain Slack bot token (settings.slack_bot_token).
Requires these Bot Token Scopes on the app + a reinstall (see SETUP.md):
  channels:read, channels:history, channels:join   (public channels)
  groups:read,   groups:history                     (private channels — the bot
                                                      must be *invited* to each)
  users:read                                         (already present)

Entry points:
  gb-slack-dryrun  — resolve channel, group by day, count + cost projection.
  gb-slack-ingest  — fetch, group by day, chunk, embed, write source='slack'.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import psycopg
import typer
from rich.console import Console
from rich.table import Table
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
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

SLACK_SOURCE = "slack"
DEFAULT_CHANNEL = "036_システム開発部"
JST = timezone(timedelta(hours=9))
# Small pages keep each response body well under the size at which large gzipped
# Slack responses get truncated mid-stream (observed IncompleteRead on full
# ~1MB bodies). 50/page paginates reliably; an internal Tier-3 app has ample
# per-minute budget.
PAGE_LIMIT = 50
# Day-windows whose real text (mentions/markup stripped) is shorter than this
# are dropped as noise — near-empty days of bare @-pings or "👍" one-liners.
MIN_CONTENT_CHARS = 15

# Non-content messages we never want in the corpus: joins/leaves, topic/purpose
# edits, and anything a bot posted.
SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "bot_message",
}


def _is_transient(exc: BaseException) -> bool:
    """urllib intermittently truncates large Slack bodies (IncompleteRead); also
    retry network errors and Slack 5xx/429. A Slack *application* error like
    not_in_channel comes back HTTP 200, so its status is not in this set and it
    propagates immediately (ensure_member relies on that)."""
    from http.client import IncompleteRead
    from urllib.error import URLError

    if isinstance(exc, (IncompleteRead, URLError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, SlackApiError):
        try:
            return exc.response.status_code in (429, 500, 502, 503, 504)
        except Exception:
            return False
    return False


_SLACK_RETRY = retry(
    stop=stop_after_attempt(8),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception(_is_transient),
    reraise=True,
)


@dataclass
class DayDoc:
    key: date  # window start (JST)
    messages: list[dict] = field(default_factory=list)  # chronological, human only

    @property
    def latest_ts(self) -> str:
        return max(m["ts"] for m in self.messages)

    @property
    def first_ts(self) -> str:
        return min(m["ts"] for m in self.messages)


class SlackClient:
    def __init__(self) -> None:
        token = settings.slack_bot_token
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN is not set (see config.py / .env).")
        self._c = WebClient(token=token)
        self._workspace_url = self._api("auth_test")["url"]  # e.g. https://foo.slack.com/

    @_SLACK_RETRY
    def _api(self, method: str, **kwargs):
        return getattr(self._c, method)(**kwargs)

    def resolve_channel(self, name: str) -> tuple[str, str, bool]:
        """Return (channel_id, channel_name, is_private). Raises if not found."""
        name = name.lstrip("#")
        cursor = None
        while True:
            resp = self._api(
                "conversations_list",
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=PAGE_LIMIT,
                cursor=cursor,
            )
            for ch in resp["channels"]:
                if ch.get("name") == name:
                    return ch["id"], ch["name"], bool(ch.get("is_private"))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        raise RuntimeError(
            f"Channel '#{name}' not found. If it is private, the bot must be "
            f"invited to it first (/invite @Gastrobrain) and the app must hold "
            f"the groups:read scope."
        )

    def ensure_member(self, channel_id: str, is_private: bool) -> None:
        """conversations.history needs the bot to be in the channel. Public:
        self-join. Private: must already have been invited."""
        try:
            self._api("conversations_history", channel=channel_id, limit=1)
            return
        except SlackApiError as e:
            if e.response.get("error") != "not_in_channel":
                raise
        if is_private:
            raise RuntimeError(
                "Bot is not in this private channel. Invite it manually: "
                "open the channel → /invite @Gastrobrain, then re-run."
            )
        self._api("conversations_join", channel=channel_id)

    def user_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        cursor = None
        while True:
            resp = self._api("users_list", limit=PAGE_LIMIT, cursor=cursor)
            for u in resp["members"]:
                prof = u.get("profile", {})
                out[u["id"]] = (
                    prof.get("display_name") or prof.get("real_name") or u.get("name") or u["id"]
                )
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return out

    def collect_messages(self, channel_id: str) -> list[dict]:
        """All human messages in the channel, deduped by ts. Threads are
        discouraged here, but if a message anchors a reply chain we pull it too
        so nothing is missed; dedup-by-ts keeps reply-broadcasts from doubling."""
        by_ts: dict[str, dict] = {}
        cursor = None
        while True:
            resp = self._api("conversations_history", channel=channel_id, limit=PAGE_LIMIT, cursor=cursor)
            for m in resp["messages"]:
                if _is_human(m):
                    by_ts.setdefault(m["ts"], m)
                if int(m.get("reply_count", 0)) > 0:
                    for r in self._replies(channel_id, m["ts"]):
                        if _is_human(r):
                            by_ts.setdefault(r["ts"], r)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return list(by_ts.values())

    def _replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        msgs: list[dict] = []
        cursor = None
        while True:
            resp = self._api(
                "conversations_replies", channel=channel_id, ts=thread_ts, limit=PAGE_LIMIT, cursor=cursor
            )
            msgs.extend(resp["messages"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return msgs

    def permalink(self, channel_id: str, ts: str) -> str:
        return f"{self._workspace_url}archives/{channel_id}/p{ts.replace('.', '')}"

    def list_channels(self) -> list[dict]:
        """Every channel the bot can see: all public channels + the private
        channels it has been invited to (Slack only returns member-of privates).
        Each dict has id / name / is_private / is_member."""
        out: list[dict] = []
        cursor = None
        while True:
            resp = self._api(
                "conversations_list",
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=PAGE_LIMIT,
                cursor=cursor,
            )
            for ch in resp["channels"]:
                out.append(
                    {
                        "id": ch["id"],
                        "name": ch.get("name", ""),
                        "is_private": bool(ch.get("is_private")),
                        "is_member": bool(ch.get("is_member")),
                    }
                )
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return out

    def channel_members(self, channel_id: str) -> list[str]:
        """Slack user ids in a channel (the bot must be a member for privates)."""
        out: list[str] = []
        cursor = None
        while True:
            resp = self._api("conversations_members", channel=channel_id, limit=PAGE_LIMIT, cursor=cursor)
            out.extend(resp.get("members", []))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return out

    def users_with_email(self) -> list[tuple[str, str]]:
        """(email, user_id) for every real (non-bot, non-deleted) member that has
        an email. Requires the users:read.email scope; without it emails are
        absent and the list comes back empty."""
        out: list[tuple[str, str]] = []
        cursor = None
        while True:
            resp = self._api("users_list", limit=PAGE_LIMIT, cursor=cursor)
            for u in resp["members"]:
                if u.get("is_bot") or u.get("deleted") or u.get("id") == "USLACKBOT":
                    continue
                email = (u.get("profile", {}) or {}).get("email")
                if email:
                    out.append((email, u["id"]))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return out


def _is_human(m: dict) -> bool:
    if m.get("bot_id") or m.get("subtype") in SKIP_SUBTYPES:
        return False
    return bool((m.get("text") or "").strip())


_MARKUP_RE = re.compile(r"<([^>\n]+)>")
_MENTION_TOKEN_RE = re.compile(r"[@#]\S+")


def _unwrap(text: str, umap: dict[str, str]) -> str:
    """Turn Slack's `<...>` markup into plain readable text:
    <@U123> / <@U123|name> → @name, <#C1|name> → #name, <!subteam^S1|name> →
    @name, <!here>/<!channel> → @here/@channel, <url|label> → label, <url> → url.
    Then unescape Slack's HTML entities (&amp; &lt; &gt;)."""
    def repl(m: re.Match) -> str:
        ref, _, label = m.group(1).partition("|")
        if ref.startswith("@"):
            return "@" + (label or umap.get(ref[1:], ref[1:]))
        if ref.startswith("#"):
            return "#" + (label or ref[1:])
        if ref.startswith("!subteam^"):
            return "@" + (label or "team")
        if ref.startswith("!"):
            return "@" + ref[1:].partition("^")[0]
        return label or ref

    return html.unescape(_MARKUP_RE.sub(repl, text))


def _clean(text: str, umap: dict[str, str]) -> str:
    return _unwrap((text or "").strip(), umap)


def _bucket_key(ts: str, group_days: int) -> date:
    """JST date of the window a message falls in. group_days=1 → its day.
    Wider windows are anchored on the proleptic ordinal so the same message
    always lands in the same bucket across runs (stable external_id)."""
    d = datetime.fromtimestamp(float(ts), JST).date()
    if group_days <= 1:
        return d
    o = d.toordinal()
    return date.fromordinal(o - (o % group_days))


def _bucket(messages: list[dict], group_days: int) -> list[DayDoc]:
    buckets: dict[date, list[dict]] = defaultdict(list)
    for m in messages:
        buckets[_bucket_key(m["ts"], group_days)].append(m)
    docs = [DayDoc(key=k, messages=sorted(v, key=lambda m: float(m["ts"]))) for k, v in buckets.items()]
    docs.sort(key=lambda d: d.key, reverse=True)  # newest-first
    return docs


def _content_len(doc: DayDoc, umap: dict[str, str]) -> int:
    joined = " ".join(_clean(m.get("text", ""), umap) for m in doc.messages)
    return len("".join(_MENTION_TOKEN_RE.sub("", joined).split()))


def _is_substantive(doc: DayDoc, umap: dict[str, str]) -> bool:
    return _content_len(doc, umap) >= MIN_CONTENT_CHARS


def _render(doc: DayDoc, umap: dict[str, str]) -> str:
    """Speaker-per-turn so chunk_transcript's _SPEAKER_RE ("<name> HH:MM") fires
    on each message and packs the day turn-by-turn."""
    lines: list[str] = []
    for m in doc.messages:
        name = umap.get(m.get("user", ""), m.get("user", "unknown"))
        hhmm = datetime.fromtimestamp(float(m["ts"]), JST).strftime("%H:%M")
        lines.append(f"{name} {hhmm}\n{_clean(m.get('text', ''), umap)}")
    return "\n\n".join(lines)


def _title(channel_name: str, doc: DayDoc, group_days: int) -> str:
    if group_days <= 1:
        return f"#{channel_name}・{doc.key.isoformat()}"
    end = doc.key + timedelta(days=group_days - 1)
    return f"#{channel_name}・{doc.key.isoformat()}〜{end.isoformat()}"


def dryrun(
    channel: str = typer.Option(DEFAULT_CHANNEL, help="Channel name (no leading #). Ignored with --all-channels."),
    all_channels: bool = typer.Option(
        False, "--all-channels",
        help="Project across every channel the bot can read instead of one --channel.",
    ),
    group_days: int = typer.Option(1, help="Days per conversation document (1 = per day)."),
) -> None:
    """Resolve channel(s), group messages by day, estimate embed cost. No
    embedding, no DB writes."""
    client = SlackClient()
    umap = client.user_map()

    if all_channels:
        targets = [(c["id"], c["name"], c["is_private"]) for c in client.list_channels()]
        console.print(f"[bold]All-channels projection:[/bold] {len(targets)} channels")
    else:
        channel_id, channel_name, is_private = client.resolve_channel(channel)
        targets = [(channel_id, channel_name, is_private)]

    grand_docs = grand_msgs = grand_tokens = 0
    t = Table("channel", "priv", "day-docs", "msgs", "~tokens", "~$", title="Per-channel projection")
    for cid, cname, priv in targets:
        try:
            client.ensure_member(cid, priv)
        except Exception as e:
            console.print(f"  [yellow]skip #{cname}: {e}[/yellow]")
            continue
        messages = client.collect_messages(cid)
        docs = [d for d in _bucket(messages, group_days) if _is_substantive(d, umap)]
        if not docs:
            continue
        tokens = sum(_estimate_tokens(_render(d, umap)) for d in docs)
        n_msgs = sum(len(d.messages) for d in docs)
        grand_docs += len(docs); grand_msgs += n_msgs; grand_tokens += tokens
        t.add_row(f"#{cname}", "🔒" if priv else "", str(len(docs)), str(n_msgs),
                  f"{tokens:,}", f"{tokens * COHERE_USD_PER_1M / 1_000_000:.2f}")

    console.print(t)
    console.print(
        f"\n[bold]Total:[/bold] {grand_docs} day-docs, {grand_msgs} messages, "
        f"~{grand_tokens:,} embed tokens (~${grand_tokens * COHERE_USD_PER_1M / 1_000_000:.2f}). "
        f"[dim]Rough; the ingest run reports actuals.[/dim]"
    )


@dataclass
class _RunState:
    """Shared budget/counters across a run — one channel or all of them, so the
    token budget and --limit are global caps, not per-channel."""
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


def _ingest_channel(client: SlackClient, channel_id: str, channel_name: str,
                    umap: dict[str, str], group_days: int, st: _RunState) -> None:
    """Fetch, group by day, embed and persist one channel into the shared state."""
    folder_path = ["Slack", channel_name]
    messages = client.collect_messages(channel_id)
    all_docs = _bucket(messages, group_days)
    docs = [d for d in all_docs if _is_substantive(d, umap)]
    st.skipped_noise += len(all_docs) - len(docs)
    console.print(
        f"[bold]#{channel_name}[/bold]: {len(messages)} msgs → {len(all_docs)} day-docs "
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
        body = _render(d, umap)
        chs = chunk_transcript(body)
        if not chs:
            st.skipped_empty += 1
            continue

        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        external_id = f"{channel_id}:{d.key.isoformat()}"
        title = _title(channel_name, d, group_days)
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

        url = client.permalink(channel_id, d.first_ts)
        updated_at = datetime.fromtimestamp(float(d.latest_ts), timezone.utc)
        try:
            n = _persist_with_retry(external_id, title, url, body, updated_at, d.key,
                                    chs, titled, embeddings, content_hash)
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            console.print(f"  [red]persist failed for {title[:40]}: {type(e).__name__}[/red]")
            continue
        st.ingested += 1
        st.chunks_total += n

        if st.ingested % 25 == 0:
            spent = st.running_tokens * COHERE_USD_PER_1M / 1_000_000
            console.print(f"  ...ingested {st.ingested} day-docs, {st.chunks_total} chunks, ~{st.running_tokens:,} tokens (~${spent:.2f})")
        if st.limit and st.ingested >= st.limit:
            st.stop = True
            st.stop_reason = f"hit --limit {st.limit}"
            return


def ingest(
    channel: str = typer.Option(DEFAULT_CHANNEL, help="Channel name (no leading #). Ignored with --all-channels."),
    all_channels: bool = typer.Option(
        False, "--all-channels",
        help="Ingest every channel the bot can read (all public + invited private) instead of one --channel.",
    ),
    group_days: int = typer.Option(1, help="Days per conversation document (1 = per day)."),
    token_budget: int = typer.Option(
        25_000_000,
        help="Hard stop if running token total would exceed this. 25M ≈ $2.50 at $0.10/1M.",
    ),
    limit: int = typer.Option(0, help="Stop after ingesting this many day-docs total (0 = no cap)."),
    batch_size: int = typer.Option(96, help="Cohere embed batch size (max 96)"),
) -> None:
    """Backfill Slack into Supabase as source='slack', one document per day-window.
    Idempotent: a day already present (by content hash) is skipped; a day that
    gained messages re-ingests. The token budget and --limit are global across
    all channels in --all-channels mode."""
    budget_usd = token_budget * COHERE_USD_PER_1M / 1_000_000
    client = SlackClient()
    umap = client.user_map()

    if all_channels:
        targets = [(c["id"], c["name"], c["is_private"]) for c in client.list_channels()]
        console.print(
            f"[bold]All-channels mode:[/bold] {len(targets)} channels   "
            f"[bold]Budget:[/bold] {token_budget:,} tokens (~${budget_usd:.2f})"
        )
    else:
        channel_id, channel_name, is_private = client.resolve_channel(channel)
        targets = [(channel_id, channel_name, is_private)]
        console.print(f"[bold]Channel:[/bold] {channel}   [bold]Budget:[/bold] {token_budget:,} tokens (~${budget_usd:.2f})")

    st = _RunState(token_budget=token_budget, limit=limit, batch_size=batch_size)
    for cid, cname, priv in targets:
        if st.stop:
            break
        try:
            client.ensure_member(cid, priv)  # public: self-join; private: must already be invited
        except Exception as e:
            console.print(f"  [yellow]skip #{cname}: {e}[/yellow]")
            continue
        _ingest_channel(client, cid, cname, umap, group_days, st)

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
            (SLACK_SOURCE, external_id, content_hash),
        )
        return cur.fetchone() is not None


def _persist(cur, external_id, title, url, folder_path, body, updated_at, meeting_date,
             chunks, titled, embeddings, content_hash) -> int:
    cur.execute(
        "SELECT id FROM documents WHERE source = %s AND external_id = %s",
        (SLACK_SOURCE, external_id),
    )
    # external_id is "<channel_id>:<YYYY-MM-DD>"; the channel id is the access key
    # for the visibility gate (see migrations/012, retrieve._ACCESS_CLAUSE).
    slack_channel_id = external_id.split(":", 1)[0]
    existing = cur.fetchone()
    if existing:
        doc_id = existing[0]
        cur.execute(
            """UPDATE documents
               SET title=%s, url=%s, author=%s, folder_path=%s,
                   updated_at=%s, raw_markdown=%s, content_hash=%s,
                   meeting_date=%s, slack_channel_id=%s, deleted_at=NULL
               WHERE id=%s""",
            (title, url, None, folder_path, updated_at, body, content_hash,
             meeting_date, slack_channel_id, doc_id),
        )
        cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
    else:
        cur.execute(
            """INSERT INTO documents
                  (source, external_id, title, folder_path, url, author,
                   updated_at, raw_markdown, content_hash, meeting_date, slack_channel_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (SLACK_SOURCE, external_id, title, folder_path, url, None,
             updated_at, body, content_hash, meeting_date, slack_channel_id),
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
