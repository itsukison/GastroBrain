-- Server-only Recall state. No browser/Data API grants or SECURITY DEFINER code.
BEGIN;
CREATE TABLE recall_runs (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL UNIQUE REFERENCES meetings(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    meet_url text NOT NULL,
    bot_id uuid UNIQUE,
    state text NOT NULL DEFAULT 'creating' CHECK (state IN
        ('creating','uncertain','joining','live','stopping','finishing','done','failed')),
    launch_hash text UNIQUE,
    launch_expires_at timestamptz NOT NULL,
    session_hash text UNIQUE,
    expires_at timestamptz NOT NULL,
    heartbeat_at timestamptz,
    healthy_at timestamptz,
    admitted_at timestamptz,
    ended_at timestamptz,
    next_seq integer NOT NULL DEFAULT 0,
    problem text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    next_check_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX recall_one_active_url ON recall_runs(meet_url)
    WHERE state IN ('creating','uncertain','joining','live','stopping');
CREATE INDEX recall_runs_user_idx ON recall_runs(user_id, created_at DESC);
CREATE INDEX recall_runs_conversation_idx ON recall_runs(conversation_id);
CREATE INDEX recall_runs_work_idx ON recall_runs(next_check_at)
    WHERE state NOT IN ('done','failed');

CREATE TABLE recall_events (
    id text PRIMARY KEY,
    bot_id uuid NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    error_code text
);
CREATE INDEX recall_events_pending_idx ON recall_events(available_at, created_at)
    WHERE processed_at IS NULL;
CREATE INDEX recall_events_bot_idx ON recall_events(bot_id);

CREATE TABLE recall_transcripts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES recall_runs(id) ON DELETE CASCADE,
    recording_id uuid,
    started_at timestamptz,
    ready boolean NOT NULL DEFAULT false,
    imported_at timestamptz,
    failed boolean NOT NULL DEFAULT false
);
CREATE INDEX recall_transcripts_run_idx ON recall_transcripts(run_id);
CREATE TABLE recall_captions (
    run_id uuid NOT NULL REFERENCES recall_runs(id) ON DELETE CASCADE,
    source_key text NOT NULL,
    PRIMARY KEY (run_id, source_key)
);
CREATE TABLE recall_commands (
    id text PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES recall_runs(id) ON DELETE CASCADE,
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '30 seconds',
    claimed_at timestamptz,
    acked_at timestamptz
);
CREATE INDEX recall_commands_pending_idx ON recall_commands(run_id, created_at)
    WHERE claimed_at IS NULL;

ALTER TABLE recall_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall_transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall_captions ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall_commands ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON recall_runs, recall_events, recall_transcripts, recall_captions,
    recall_commands FROM PUBLIC, anon, authenticated;
COMMIT;
