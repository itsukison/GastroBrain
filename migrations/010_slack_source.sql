-- 010_slack_source.sql — allow source='slack' on documents.
--
-- The Slack channel-thread ingestion path (gb-slack-ingest,
-- src/gastrobrain/slack_ingest.py) writes documents with source='slack'.
-- The documents_source_check constraint (widened to 'gdrive' in 008) must
-- also permit 'slack' or any ingest attempt fails on the CHECK. Mirror 008.

BEGIN;

ALTER TABLE documents DROP CONSTRAINT documents_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_source_check
    CHECK (source = ANY (ARRAY['notepm'::text, 'manual'::text, 'gdrive'::text, 'slack'::text]));

COMMIT;
