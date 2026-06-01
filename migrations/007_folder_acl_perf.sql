-- 007_folder_acl_perf.sql — speed up the org view's folder listing.
--
-- The org "フォルダアクセス" tab groups documents by folder_path to show
-- per-folder doc counts + effective level. Without an index that was a full
-- sequential scan of `documents` (~3.5s — the heap rows are fat because of the
-- raw_markdown column). This covering index lets the GROUP BY run as an
-- index-only scan over just (folder_path, min_level), skipping the heap.

BEGIN;

CREATE INDEX IF NOT EXISTS documents_folder_level_idx
    ON documents (folder_path, min_level)
    WHERE deleted_at IS NULL;

COMMIT;
