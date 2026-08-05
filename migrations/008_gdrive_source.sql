-- 008_gdrive_source.sql — allow source='gdrive' on documents.
--
-- The Google Drive meeting-transcript ingestion path (gb-drive-ingest,
-- src/gastrobrain/gdrive_cli.py) writes documents with source='gdrive', but
-- the original documents_source_check only permitted 'notepm' and 'manual'.
-- Any ingest attempt therefore failed on the CHECK constraint. Widen the
-- constraint so the existing (already-committed) Drive code can run.

BEGIN;

ALTER TABLE documents DROP CONSTRAINT documents_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_source_check
    CHECK (source = ANY (ARRAY['notepm'::text, 'manual'::text, 'gdrive'::text]));

COMMIT;
