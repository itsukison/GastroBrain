# Recall guest-bot pilot

Implementation: 2026-09-22. The guest bot reuses Gastrobrain's Realtime voice
agent and scoped RAG pipeline. Calendar scheduling and Google sign-in are deferred.
The Mac/Meetron path remains available. See `../../meetron/RECALL_PLAN.md` for the
design and live acceptance checklist.

## Operation

When enabled, `/meetings` has a Google Meet URL/title/attendee form. A company
operator starts a bot named 商談AI and admits it from Meet. The operator is added
to the meeting's readers; explicit attendee emails can also read the record.
The bot's RAG access is the initiating operator's current access scope.

The bot loads `/voice/recall` in Recall Output Media. Its default microphone is
the meeting audio; an audio element returns speech to the room. The camera shows
商談AI, the awake/standby state, wake/quiet command hints, and search/response
activity. The latest RAG answer appears with up to two reference document titles
and a count of additional sources; source URLs, snippets, account UI, and raw
errors are excluded. This is a read-only camera view. The normal website retains
clickable sources. Wake words, typed commands, follow-ups and the
90-second idle timeout use the existing meeting gate. Voice rotates at 55 minutes
using the same run, meeting and conversation. Three failed starts within three
minutes stop page recovery; a 180-second health watchdog asks the bot to leave.

The pilot reserves the display name 商談AI for the bot. Fully played responses
are stored once by response ID with playback start/end times. Matching Recall
captions are reconciled using text and timing while retaining source evidence.
Interrupted playback relies on captions. Validate playback and echo behavior in
a live call; a page crash can lose the browser copy of a spoken turn.

Japanese meeting captions are ingested live, then reconciled against the final
Recall transcript. A missing/failed transcript is surfaced on the run and does
not produce a successful empty summary. Finalization orders segments by their
original recording timestamps before the existing summary/Q&A code reads them.

## Security and delivery

- Human run routes require the existing Supabase login. Launch credentials expire
  after 15 minutes and exchange once for an HttpOnly, Secure, same-site cookie.
  The launch credential is in a URL fragment, removed before any page API call.
- Bot requests recheck the bound user, meeting membership, conversation ownership,
  expiry and revocation. The caller cannot choose a user, conversation or meeting.
  Ordinary account routes remain protected by the human login.
- Runtime Recall and OpenAI keys stay server-side. Secrets are not put in the
  output page, logs, Git or the meeting service token.
- Webhooks verify the raw signature and timestamp before committing an inbox
  row. Background processing is an optimization; the scheduled minute drain is
  required for recovery after instance loss. Row locks work with transaction pooling.
- Duplicate starts reuse the current run. Ambiguous creation is reconciled by
  metadata; it is never blindly retried. An unresolved create stays visible and
  requires operator investigation before another run can use that Meet URL.
- Chat commands expire 30 seconds after their source timestamp. Claims are
  at-most-once: a crash between claim and acknowledgement loses the command
  instead of repeating a question aloud. The participant can ask again.
- Explicit stop revokes the bot grant immediately and queues the leave request.
  Recall's own recording limit defaults to two hours as a separate exit bound.

## Deployment

### Transcript reconciliation rollout

Production migration applied 2026-09-24 UTC: existing transcript rows preserved,
RLS verified enabled, and browser-role SELECT privileges verified absent.

For a new environment, apply `migrations/20260924033828_recall_transcript_sources.sql` after migration
015 and **before deploying the updated backend**. The CLI-generated migration
is stored in this repository's existing `migrations/` directory. It extends the
private caption ledger with source/transcript/participant metadata, raw evidence,
playback intervals and a link to the displayed transcript row. Existing RLS and
the absence of browser-role grants remain unchanged.

Roll out between calls: let existing runs finalize before deploying the backend,
then deploy the frontend so it can submit `ended_at`. Older frontends remain
accepted but their untimed speech cannot suppress unknown captions. New frontend
payloads require the updated backend. Do not roll the backend back while new bot
pages are running, since the old request schema rejects `ended_at`.

The final Recall download replaces its live captions atomically; late events
cannot re-add them. The browser saves one fully played response with its actual
playback-event interval. Unknown captions are reconciled using timing and strong
text evidence, including split phrases; named human speech and ambiguous short
acknowledgements remain. Interrupted playback relies on Recall captions.
Raw source evidence survives reconciliation and remains server-only.

Historical rows are retained without guessing provenance. This migration does
not repair existing duplicated meetings or regenerate their summaries.
See `TRANSCRIPT_DUPLICATION_ANALYSIS.md` for evidence and limitations. Verify a
new live call before relying on timing-based reconciliation in production.

### Initial pilot setup

1. Apply `migrations/015_recall_runs.sql`. These five private tables have RLS and
   no grants to anonymous/authenticated Data API roles. Use the backend DB role.
2. Provision Secret Manager values `RECALL_API_KEY`,
   `RECALL_WEBHOOK_VERIFICATION_SECRET`, and a random `RECALL_WORKER_TOKEN`.
   Grant the existing Cloud Run service account access to these secrets.
3. Deploy with the existing script. It preserves existing env/secret bindings:

   ```sh
   RECALL_ENABLED=true \
   PUBLIC_API_BASE_URL=https://gastrobrain-rjp7bbdhta-an.a.run.app \
   RECALL_WEB_URL=https://gastron-brain-web.vercel.app \
   ./deploy/run.sh
   ```

4. Run `.venv/bin/python deploy/recall_scheduler.py --api-url
   https://gastrobrain-rjp7bbdhta-an.a.run.app` to create/update the minute drain.
   The script reads its worker token directly from Secret Manager without putting
   it in command arguments. Scheduler calls `POST /v1/recall/drain`.
5. Register `/v1/recall/webhook` in the **ap-northeast-1** Recall workspace for
   `bot.joining_call`, `bot.in_waiting_room`, `bot.in_call_not_recording`,
   `bot.in_call_recording`, `bot.call_ended`, `bot.done`, `bot.fatal`,
   `recording.done`, `recording.failed`, `transcript.done`, `transcript.failed`.
   Send a signed test delivery and confirm durable receipt.
6. Push to the existing GitHub `main` → Vercel production connection. No new web
   secret is needed: its existing API URL and OpenAI key are reused.
7. Test the live guest and a call crossing 55 minutes. Automated tests do not
   prove Meet admission, room audio, echo suppression, caption coverage or latency.

Disable new/current bot requests with `RECALL_ENABLED=false`, but first stop any
active runs while the integration is enabled. Keep the scheduler and receiver
running until finalization completes. Rollback does not require dropping tables.

## Checks

`uv sync --group dev` installs the test dependencies. `pytest` starts isolated
Postgres for Recall boundary tests; it never uses the production DB. Run
`npm test` and `npm run build` under `web/` for the controller tests and build.

The boundary tests cover grants, ownership/membership revocation, expiry, actual
entrypoint routing, signed inbox delivery, duplicate creation, early lifecycle
events, final transcript tails, failed transcripts, single summary, command
claims/expiry, state synchronization, manual stop and health shutdown. RAG calls
are routed into the existing `voice_ask` under the verified run identity.

Deployment and live-pilot evidence is recorded in `../../meetron/RECALL_PLAN.md`.

## Camera display

`web/src/components/recall-display.tsx` is shared by launch and live session
screens. Unavailable sessions show preparation/reconnection/failure/end states
instead of a stale awake indicator. Quiet changes future response eligibility;
it does not cancel speech already playing, so standby and response activity can
appear together. Playback events keep the response indicator visible after
generation has finished.

The answer card is scoped to an accepted question and lookup. Ordinary room
speech does not dismiss it. A new accepted question, failed search, or session
stop/restart clears it; late results from an older question/session cannot replace
the current card. Direct answers from the live conversation currently have no
answer card. Reference titles represent the documents supplied to RAG, not a
claim-by-claim citation mapping, and are labeled 参照資料.

For offline visual review, run `node scripts/preview-recall-display.mjs` from
`web/`. It renders the actual component with twelve synthetic scenarios into
`/tmp/recall-display-preview`, without credentials, microphone use, or a bot.
Review at 1280×720, 640×360 and 320×180. Long answers/titles are clamped; pin the
tile to read source details. Frontend tests cover card lifetime, stale results,
accepted-turn notifications, and controller availability transitions.

The display and transcript fixes ship together. A live camera/audio and caption
reconciliation check remains required after rollout. Automated verification:
70 backend tests, 68 frontend tests, typecheck and production build passed.
