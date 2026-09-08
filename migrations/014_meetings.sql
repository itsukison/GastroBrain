-- 014_meetings.sql — the 商談AI meeting surface: meetings, their transcripts,
-- and who may read them. See docs/MEETINGS_WEB.md.
--
-- The AI participant runs on a VPS (../meetron) and posts here over HTTP with a
-- service token; the browser reads through the normal user JWT. Nothing writes
-- to Postgres directly from the VPS.
--
-- Access is per-meeting and keyed by EMAIL, not user_id: the Calendar invite is
-- how a meeting is created (meetron/AGENTS.md §5.1), so its attendee list is the
-- participant list, and an invited colleague may not have signed into
-- Gastrobrain yet — there is no auth.users row to point at. Email is already the
-- identity key on every other surface (docs/ACCESS_CONTROL.md §1). Fail-closed:
-- an email that is not a participant sees nothing.
--
-- Meeting transcripts are MORE sensitive than the corpus they sit next to: HR,
-- 経理 and 法務 notebooks are excluded at ingestion (config/notepm_excluded_notes.yaml)
-- but an internal meeting can contain all three verbatim. Hence no company-wide
-- default.

BEGIN;

-- ---------------------------------------------------------------------------
-- One row per meeting the AI is booked into or has joined.
--
-- Created by the calendar watcher when it discovers the invite (status
-- 'scheduled'), then updated by the participant as it joins and leaves. Upserts
-- key on google_event_id: the watcher re-posts the same event on every poll.
-- ---------------------------------------------------------------------------

CREATE TABLE meetings (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    google_event_id   TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL DEFAULT '(無題の会議)',
    meet_url          TEXT,
    scheduled_at      TIMESTAMPTZ NOT NULL,
    started_at        TIMESTAMPTZ,          -- when the AI actually joined
    ended_at          TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'scheduled'
                      CHECK (status IN ('scheduled', 'joining', 'live', 'ended', 'failed')),

    summary           TEXT,
    next_actions      JSONB,
    -- Distinguishes "the summary job is still running" from "it failed", which
    -- `summary IS NULL` alone cannot. Without it a failed job shows 生成中 forever.
    summary_status    TEXT NOT NULL DEFAULT 'pending'
                      CHECK (summary_status IN ('pending', 'ready', 'failed')),

    -- asleep = listens and transcribes, never speaks (default).
    -- open    = answers without needing its name.
    -- The 90 s expiry out of `open` is enforced on the meeting side, which
    -- PATCHes us back to asleep. Do not add a second timer here.
    agent_state       TEXT NOT NULL DEFAULT 'asleep'
                      CHECK (agent_state IN ('asleep', 'open')),

    -- Set when a human renames the meeting in the UI. The calendar watcher
    -- re-posts the same event on every poll, so without this the next poll
    -- would silently undo the rename.
    renamed_at        TIMESTAMPTZ,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ
);

-- The list page: newest first, live meetings included.
CREATE INDEX meetings_scheduled_at_idx
    ON meetings (scheduled_at DESC)
    WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- Who may read a meeting. One row per invited email, lowercased.
--
-- Rebuilt wholesale from the Calendar attendee list on every upsert of the
-- parent meeting, plus rows added by the UI's "share with…" action — those
-- carry added_by so a re-sync from Calendar does not silently drop them.
-- ---------------------------------------------------------------------------

CREATE TABLE meeting_participants (
    meeting_id    UUID NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    is_organizer  BOOLEAN NOT NULL DEFAULT false,
    -- NULL = came from the Calendar invite. Set = shared by hand from the UI.
    added_by      TEXT,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (meeting_id, email),
    CONSTRAINT meeting_participants_email_lower CHECK (email = lower(email))
);

-- "Which meetings can this person see" is the hot query — lead with email.
CREATE INDEX meeting_participants_email_idx
    ON meeting_participants (email, meeting_id);

-- ---------------------------------------------------------------------------
-- The transcript: one row per caption line, speaker-labelled.
--
-- `seq` is monotonic per meeting and assigned by the participant. It is the
-- dedupe key: the VPS retries on network failure and may resend a whole batch,
-- so writes are ON CONFLICT DO NOTHING. Batches may arrive out of order.
-- ---------------------------------------------------------------------------

CREATE TABLE meeting_segments (
    id          BIGSERIAL PRIMARY KEY,
    meeting_id  UUID NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    speaker     TEXT NOT NULL,
    text        TEXT NOT NULL,
    spoken_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (meeting_id, seq)
);

-- ---------------------------------------------------------------------------
-- "Ask a question about this meeting" reuses the existing chat stack.
--
-- The FK points this way round on purpose. `conversations.user_id` is NOT NULL
-- with RLS `user_id = auth.uid()`, so a single shared conversation per meeting
-- would be readable only by whoever happened to own it. One thread per person
-- per meeting keeps ownership, RLS, streaming, citations and feedback exactly
-- as they already work.
-- ---------------------------------------------------------------------------

ALTER TABLE conversations
    ADD COLUMN meeting_id UUID REFERENCES meetings (id) ON DELETE SET NULL;

-- The Q&A tab: this user's threads about this meeting.
CREATE INDEX conversations_meeting_idx
    ON conversations (meeting_id, user_id, updated_at DESC)
    WHERE meeting_id IS NOT NULL AND deleted_at IS NULL;

-- Keep meetings.updated_at honest so the list can order by activity later.
CREATE OR REPLACE FUNCTION touch_meeting_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER meetings_touch_updated_at
BEFORE UPDATE ON meetings
FOR EACH ROW EXECUTE FUNCTION touch_meeting_updated_at();

-- ---------------------------------------------------------------------------
-- Row-level security — defence-in-depth, exactly as in 002_web_chat.sql.
-- FastAPI ALSO filters on the caller's email in every query; that is the
-- control, this is the backstop for direct DB access.
--
-- auth.jwt() ->> 'email' rather than a join through auth.users: the email claim
-- is in the token FastAPI already verified, and it is what the app filters on,
-- so the two checks cannot drift.
-- ---------------------------------------------------------------------------

ALTER TABLE meetings             ENABLE ROW LEVEL SECURITY;
ALTER TABLE meeting_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE meeting_segments     ENABLE ROW LEVEL SECURITY;

CREATE POLICY meeting_participant_select
    ON meetings FOR SELECT
    USING (
        deleted_at IS NULL
        AND EXISTS (
            SELECT 1 FROM meeting_participants p
            WHERE p.meeting_id = meetings.id
              AND p.email = lower(auth.jwt() ->> 'email')
        )
    );

CREATE POLICY meeting_participants_select
    ON meeting_participants FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM meeting_participants mine
            WHERE mine.meeting_id = meeting_participants.meeting_id
              AND mine.email = lower(auth.jwt() ->> 'email')
        )
    );

CREATE POLICY meeting_segments_select
    ON meeting_segments FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM meeting_participants p
            WHERE p.meeting_id = meeting_segments.meeting_id
              AND p.email = lower(auth.jwt() ->> 'email')
        )
    );

COMMIT;
