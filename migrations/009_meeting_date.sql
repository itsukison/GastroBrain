-- 009_meeting_date.sql — structured meeting date for Drive transcripts.
--
-- Drive transcript titles carry the meeting datetime (e.g.
-- "キャニオンスパイス様打ち合わせ - 2026-06-10 14:13:15"), but it was only
-- searchable as free text — so date-scoped questions ("4/3 の会議で何を話した")
-- couldn't be answered reliably. Store the date in a column so retrieval can
-- filter on it. NULL for sources without a meeting date (notepm, manual).

BEGIN;

ALTER TABLE documents ADD COLUMN meeting_date date;

CREATE INDEX documents_meeting_date
    ON documents (meeting_date)
    WHERE meeting_date IS NOT NULL;

COMMIT;
