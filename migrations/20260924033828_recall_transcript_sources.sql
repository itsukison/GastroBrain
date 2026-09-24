-- Add provenance without guessing the origin of pre-migration rows.
BEGIN;
ALTER TABLE recall_captions
    ADD COLUMN source text NOT NULL DEFAULT 'legacy'
        CHECK (source IN ('legacy','live','final','spoken')),
    ADD COLUMN transcript_id uuid REFERENCES recall_transcripts(id) ON DELETE CASCADE,
    ADD COLUMN segment_id bigint REFERENCES meeting_segments(id) ON DELETE SET NULL,
    ADD COLUMN participant_id text,
    ADD COLUMN speaker text,
    ADD COLUMN text text,
    ADD COLUMN started_at timestamptz,
    ADD COLUMN ended_at timestamptz,
    ADD COLUMN raw jsonb,
    ADD COLUMN superseded boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT recall_caption_interval CHECK (ended_at >= started_at);
CREATE INDEX recall_captions_transcript_idx ON recall_captions(transcript_id);
CREATE UNIQUE INDEX recall_captions_segment_idx ON recall_captions(segment_id);
CREATE INDEX recall_captions_time_idx ON recall_captions(run_id, source, started_at);
-- Existing RLS/no-browser-grants remain in force for raw transcript evidence.
COMMIT;
