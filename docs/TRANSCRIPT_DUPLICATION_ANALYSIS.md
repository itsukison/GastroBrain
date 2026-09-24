# Meeting transcript duplication investigation — 2026-09-23

Scope: the meeting detail → 文字起こし tab. The investigation below preceded
the implementation described at the end. Production rollout was authorized
after implementation; the provenance migration was applied 2026-09-24 UTC.
Historical transcript cleanup has not been performed.

## Findings

The database already contains duplicate representations of speech. The React
page groups consecutive rows by speaker and joins their text; it does not create
the additional rows. Refresh replaces the fetched data rather than appending it.

### 1. AI captions arrive as `Unknown`, bypassing the exclusion

There are two independent writers:

- `recall_worker.import_segment`: imports Recall's Google Meet captions.
- `/v1/recall/bot/spoken`: saves the browser's assistant transcript as `商談AI`
  after an output playback-stop event.

The first path excludes only `participant.name == "商談AI"`. In the inspected
production meeting, the incoming webhook itself identifies these captions as
`name: "Unknown", id: 2147483647`. The website did not invent that name.
Both paths therefore save the same speech. Their deduplication keys are unrelated:
caption-content hashes versus `spoken:<Realtime item id>`.

Concrete production evidence (meeting `c4b97a61-7b6d-49d2-ba9f-34ba0fd7032c`):

- 26 saved rows: 4 browser assistant rows and 22 Recall caption rows.
- Rows 9 (`Unknown`) and 12 (`商談AI`) contain exactly the same eight characters.
- Their recorded times are 03:26:15.378097 and 03:26:16.555 UTC, respectively.
- The matching original webhook explicitly carries the `Unknown` participant.
- Longer Unknown passages also overlap the assistant transcripts, with caption
  segmentation/transcription differences.

This confirms the user's symptom for at least one exact pair. It does **not**
establish that all Unknown participants, or this numeric participant ID in all
meetings, represent our bot. Do not globally rename or discard them.

### 2. Final transcript import appends another representation of live captions

`recall_worker.maintain` downloads the completed transcript and passes every
segment through the same append-only importer. `recall_client.caption_key`
hashes the transcript ID, participant ID, and exact list of word texts and
relative start timestamps. That identifies identical payloads, not equivalent
speech when the word/segment representation changes.

Production evidence:

- 16 live caption events produce 15 unique keys; all 15 are recorded.
- Seven additional non-`spoken:` keys exist after final transcript import.
- Five additional saved rows exactly equal the concatenated text of successive
  live events: 53, 39, 66, 47, and 75 characters, including human speech.
- Two human utterances also appear twice with identical text/start time
  (rows 1/2 and 25/26).
- Final import completed at 03:27:07.620366 UTC.

The source path and saved rows establish the final-import duplication. The raw
final download was not fetched in this investigation, so the precise field
differences causing every individual hash mismatch are not established. Merging
segments alone is sufficient to reproduce the bug; timestamp precision or word
tokenization differences would also change the key.

### 3. Assistant timestamps explain the ordering and complicate reconciliation

`voice-session.tsx` records `new Date()` after playback stops, then scans all
unsaved completed assistant items. It does not retain each item's actual speech
start/end times. In this meeting, two assistant rows were saved 1 ms apart and
another pair 2 ms apart, despite representing distinct transcript items.

Recall captions use speech-start timestamps. Finalization sorts by `spoken_at`,
so a caption naturally appears before the later-stamped assistant copy. During
the live call, row sequence instead follows insert order. A narrow time-window
deduplication fix would be unreliable with the current assistant timestamps.

## Verification

Read-only production queries compared stored rows, source keys and original
webhooks without printing conversation text. Three diagnostic tests using the
existing isolated PostgreSQL/FastAPI fixture passed:

1. Unknown caption + browser spoken turn persist as two rows with identical text.
2. Two live caption fragments + one combined final segment persist as three rows.
3. An exactly repeated caption payload is correctly deduplicated, including
   replay during final import.

The diagnostic file is `/tmp/test_recall_duplicate_diagnosis.py`; run from the
repository root with `.venv/bin/pytest -q /tmp/test_recall_duplicate_diagnosis.py`.
These tests demonstrate current defects; they do not assert a fix.

The existing replay test in `tests/test_recall.py` uses identical segment shapes
for live and final data, so it does not exercise this production case.

## Recommended implementation

1. Preserve source provenance alongside transcript rows: live/final Recall
   origin, transcript/participant identity, and browser assistant item identity.
   Keep raw input separately from the transcript presented to readers.
2. Treat the completed Recall transcript as the authoritative replacement for
   that transcript's live caption rows, in a single retry-safe transaction.
   Preserve browser assistant rows and reconcile late events so they cannot
   append the replaced live captions again.
3. Capture actual assistant playback intervals per response/item, including
   interruption and delayed transcript availability. Reconcile caption copies
   with assistant speech using reliable participant identity where available,
   and conservative text/timing evidence for unknown speakers. Preserve ambiguous
   captions and genuine human repetition rather than deleting all `Unknown` rows.
4. Fix the stored canonical record before summary/Q&A consume it. A frontend-only
   hide rule would leave duplicated evidence in those downstream consumers.
5. Add regression cases for split/merged final captions, unknown AI attribution,
   actual unknown human speech, repeated phrases, delayed delivery, retries and
   finalization. Evaluate historical repair separately with a reviewable diff;
   current rows lack complete provenance and accurate assistant intervals.

Relevant code: `src/gastrobrain/recall_worker.py:34,47,213`,
`src/gastrobrain/recall_client.py:164`, `src/gastrobrain/recall_api.py:282`,
`web/src/components/voice-session.tsx:561`,
`web/src/components/meeting-detail.tsx:451`, `src/gastrobrain/web_api.py:1574`.

Provider references checked: [Recall real-time transcript schema](https://docs.recall.ai/docs/bot-real-time-transcription)
allows nullable participant names; [meeting caption documentation](https://docs.recall.ai/docs/meeting-caption-transcription)
describes the configured native-caption source. Neither establishes a universal
mapping from `Unknown` or `2147483647` to our bot.

## Implementation

Implemented in `recall_transcript.py`, the Recall worker/API, and
`web/src/lib/recall-spoken.ts`. Apply the new provenance migration before
deploying the backend; deploy the frontend after the backend. Full rollout
instructions are in `RECALL_PILOT.md`.

- Source metadata and raw payloads are retained in private `recall_captions`.
  Removing a duplicate from `meeting_segments` does not remove its evidence.
- Final captions atomically replace live rows for the same transcript; later
  live deliveries are ignored. A failed import rolls back replacement. An empty
  final response cannot erase nonempty live evidence.
- The browser associates raw Realtime `response.done` and playback start/stop
  events by response ID. It records only completed, fully played audio, with
  start/end times captured on those events. It does not sweep SDK history.
  Cleared/cancelled audio falls back to Recall captions instead of saving the
  full partly unplayed answer.
- Caption reconciliation uses a two-second timing tolerance and substantial
  normalized text agreement. It supports adjacent fragments and small ASR
  differences. Named human speakers are never candidates. Short standalone
  unknown acknowledgements and unmatched captions remain visible. No numeric
  participant ID is treated as proof of bot identity.

This is conservative heuristic reconciliation, not guaranteed speaker identity:
a real unknown speaker saying the same longer words during AI playback remains
ambiguous. Raw evidence is retained. Live-call validation is still needed for
timing skew, caption latency and real ASR variation.

Pre-migration rows remain `legacy`; their absent source/timing metadata is not
invented. The previously inspected meeting therefore still needs a separately
reviewed historical repair. Rollout must start between calls, after old runs
have finished, because old in-flight rows cannot safely be replaced by provenance.
