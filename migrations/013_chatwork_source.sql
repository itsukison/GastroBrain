-- 013_chatwork_source.sql — allow source='chatwork' on documents.
--
-- The Chatwork room ingestion path (gb-chatwork-ingest,
-- src/gastrobrain/chatwork_ingest.py) writes documents with source='chatwork'.
-- The documents_source_check constraint (last widened to 'slack' in 010) must
-- also permit 'chatwork' or any ingest attempt fails on the CHECK. Mirror 010.
--
-- No ACL tables: Chatwork exposes no member emails and has no public-room
-- concept, so per-user gating (as for notepm/slack) is not possible. Only an
-- operator-curated allowlist of rooms (CHATWORK_ROOM_IDS) is ingested, and
-- those docs are unrestricted — the retrieve.py gate already passes any source
-- outside ('notepm','slack'), so no gate change is needed.

BEGIN;

ALTER TABLE documents DROP CONSTRAINT documents_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_source_check
    CHECK (source = ANY (ARRAY['notepm'::text, 'manual'::text, 'gdrive'::text, 'slack'::text, 'chatwork'::text]));

COMMIT;
