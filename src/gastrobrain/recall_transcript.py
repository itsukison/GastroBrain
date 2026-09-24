"""Canonical transcript and retained source evidence. Caller holds the run lock."""
import unicodedata
from datetime import timedelta
from difflib import SequenceMatcher

from psycopg.types.json import Jsonb


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if c.isalnum())


def matches_speech(caption, spoken):
    """Require substantial text evidence; never infer identity from Unknown's ID."""
    caption, spoken = normalized(caption), normalized(spoken)
    if len(caption) < 6:
        return False  # Short acknowledgements can easily belong to a human.
    if caption in spoken:
        return True
    if len(caption) < 20:
        return False
    blocks = [b for b in SequenceMatcher(None, caption, spoken, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return False
    # Allow small ASR spelling differences, but not a few shared keywords spread
    # across a long answer or a caption that contains unrelated extra speech.
    return (sum(b.size for b in blocks) / len(caption) >= .92
            and max(b.size for b in blocks) >= 10
            and blocks[-1].b + blocks[-1].size - blocks[0].b <= len(caption) * 1.2)


def insert_source(cur, run, key, speaker, text, started_at, *, source,
                  ended_at=None, transcript_id=None, participant_id=None, raw=None):
    if not text.strip():
        return
    cur.execute("""INSERT INTO recall_captions
        (run_id,source_key,source,transcript_id,participant_id,speaker,text,started_at,ended_at,raw)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING RETURNING source_key""",
        (run['id'], key, source, transcript_id, participant_id, speaker, text,
         started_at, ended_at, Jsonb(raw) if raw is not None else None))
    if not cur.fetchone():
        return
    cur.execute("UPDATE recall_runs SET next_seq=next_seq+1 WHERE id=%s RETURNING next_seq", (run['id'],))
    seq = cur.fetchone()['next_seq']
    cur.execute("""INSERT INTO meeting_segments(meeting_id,seq,speaker,text,spoken_at)
        VALUES (%s,%s,%s,%s,%s) RETURNING id""", (run['meeting_id'], seq, speaker, text, started_at))
    segment_id = cur.fetchone()['id']
    cur.execute("UPDATE recall_captions SET segment_id=%s WHERE run_id=%s AND source_key=%s",
                (segment_id, run['id'], key))


def reconcile_speech(cur, run):
    # Raw evidence survives suppression. Timed browser speech is authoritative;
    # old clients without a start/end interval cannot suppress room captions.
    cur.execute("""SELECT c.source,c.transcript_id,c.segment_id,c.participant_id,c.speaker,c.text,c.started_at,c.ended_at FROM recall_captions c
        WHERE c.run_id=%s AND c.source IN ('live','final') AND NOT c.superseded
        AND (lower(trim(c.speaker)) IN ('unknown','unknown speaker','参加者','不明') OR c.speaker='商談AI')
        AND EXISTS (SELECT 1 FROM recall_captions a WHERE a.run_id=c.run_id
            AND a.source='spoken' AND a.ended_at IS NOT NULL
            AND a.started_at <= COALESCE(c.ended_at,c.started_at)+interval '2 seconds'
            AND a.ended_at >= c.started_at-interval '2 seconds')
        ORDER BY c.started_at,c.segment_id""", (run['id'],))
    captions = cur.fetchall()
    # Include already matched evidence when grouping: a short trailing caption
    # may arrive after the longer beginning was reconciled. Keep runs separate
    # across speakers, transcripts, source versions and pauses.
    groups = []
    for caption in captions:
        if groups:
            previous = groups[-1][-1]
            same = all(caption[k] == previous[k] for k in ('speaker', 'participant_id', 'transcript_id', 'source'))
            gap = caption['started_at'] - (previous['ended_at'] or previous['started_at'])
            if same and gap <= timedelta(seconds=2):
                groups[-1].append(caption)
                continue
        groups.append([caption])
    for group in groups:
        if not any(c['segment_id'] is not None for c in group):
            continue
        cur.execute("""SELECT text,started_at,ended_at FROM recall_captions
            WHERE run_id=%s AND source='spoken' AND ended_at IS NOT NULL
            AND started_at <= %s+interval '2 seconds' AND ended_at >= %s-interval '2 seconds'
            ORDER BY started_at,source_key""",
            (run['id'], max(c['ended_at'] or c['started_at'] for c in group), group[0]['started_at']))
        nearby_speech = cur.fetchall()
        # First match the complete phrase, then independent substantial fragments.
        candidates = [group]
        # A real human contribution at the edge must not prevent matching the
        # preceding AI fragments, or be swallowed with them. Bound the search.
        for size in range(min(8, len(group) - 1), 0, -1):
            candidates.extend(group[i:i + size] for i in range(len(group) - size + 1))
        for parts in candidates:
            if not any(c['segment_id'] is not None for c in parts):
                continue
            start = min(c['started_at'] for c in parts)
            end = max(c['ended_at'] or c['started_at'] for c in parts)
            speech = [s for s in nearby_speech
                      if s['started_at'] <= end + timedelta(seconds=2)
                      and s['ended_at'] >= start - timedelta(seconds=2)]
            if not speech:
                continue
            # The whole caption must fit within the playback range, not merely touch it.
            if (start < speech[0]['started_at'] - timedelta(seconds=2)
                    or end > max(s['ended_at'] for s in speech) + timedelta(seconds=2)):
                continue
            text = ''.join(c['text'] for c in parts)
            answer = ''.join(s['text'] for s in speech)
            named_bot = all(c['speaker'] == '商談AI' for c in parts)
            if matches_speech(text, answer) or (named_bot and normalized(text) and normalized(text) in normalized(answer)):
                ids = [c['segment_id'] for c in parts if c['segment_id'] is not None]
                cur.execute("DELETE FROM meeting_segments WHERE id=ANY(%s)", (ids,))
                for c in parts:
                    c['segment_id'] = None


def replace_live(cur, transcript):
    """Atomic with final insertion/imported_at; source rows retain raw evidence."""
    cur.execute("""DELETE FROM meeting_segments WHERE id IN
        (SELECT segment_id FROM recall_captions WHERE transcript_id=%s AND source='live')""",
        (transcript['id'],))
    cur.execute("UPDATE recall_captions SET superseded=true WHERE transcript_id=%s AND source='live'",
                (transcript['id'],))
