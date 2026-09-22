# MEETINGS_WEB.md — the 商談AI web surface

> **Recall transport (2026-09-22):** the guest pilot now reuses the existing
> voice agent. See [RECALL_PILOT.md](RECALL_PILOT.md) for scoped bot routes,
> durable webhooks, start/stop controls and deployment. Existing Meetron APIs
> below are preserved.

> **Status:** spec. Written 2026-09-07; §4 and §5 settled and the migration
> written the same day. Decisions taken since are marked **decided**.
> **Audience:** the agent/engineer building the web half. This is your brief.
> **You own:** the database, the pages, the HTTP API, the Python endpoints.
> **You do not own:** anything that happens inside a Google Meet.

---

## 1. What this is

Gastroduce runs an AI participant that joins internal Google Meet meetings,
answers when addressed, and reports afterwards. The participant itself lives in
a different repo (`../meetron`) and is being built in parallel by another agent.

**Your job is everything a human sees.** Concretely: a Meetings section inside
the existing Gastrobrain site where staff can look back on past meetings, read
the transcript, read the summary, ask questions about what was said, and see
which meetings the AI is booked into next.

You are not building a new product. You are adding a section to the Next.js app
that already exists at `web/`, reusing its auth, its API plumbing, its chat
stack and its permission model.

### The split, precisely

```
        ┌──────────────────────────────┐
        │  ../meetron  (other agent)   │
        │                              │
        │  joins the Meet              │
        │  scrapes captions            │
        │  reads Meet chat             │
        │  decides when to speak       │
        └──────────────┬───────────────┘
                       │  HTTP, service token
                       │  §6 is the whole contract
                       ▼
        ┌──────────────────────────────┐
        │  YOU                         │
        │                              │
        │  FastAPI  /v1/meetings/*     │
        │  Postgres tables             │
        │  Next.js /meetings pages     │
        │  Next.js /api/meetings/*     │
        └──────────────────────────────┘
```

**Neither side blocks the other**, as long as §6 is agreed first. Build against
the contract with fake data; the meeting agent will start posting real data into
it. If you need to change §6, say so early — it is the only shared surface.

---

## 2. Read these first, in this order

| Doc | Why |
|---|---|
| `docs/WEB_ARCHITECTURE.md` | **Mandatory.** How auth, the `/api/*` proxy, SSE, prompts and per-thread memory actually work. Everything you build follows these patterns. |
| `CLAUDE.md` (repo root) | House rules: simplicity, surgical diffs, no speculative abstraction. They are enforced here. |
| `docs/ACCESS_CONTROL.md` | You will need this for §5. |
| `../meetron/AGENTS.md` §1, §5.2, §6 | Context on the meeting side and the decisions already taken. Skim; do not implement from it. |
| `docs/VOICE_AGENT_PLAN.md` | Only if you touch `/voice`. You probably should not. |

---

## 3. What already exists — reuse, do not rebuild

The app is Next.js 15 (App Router, React 19), `@assistant-ui/react`,
`@supabase/ssr`, Tailwind v4. Backend is FastAPI on Cloud Run, Postgres on
Supabase.

| You need | It already exists | Where |
|---|---|---|
| Login | Slack OIDC → Supabase session, `@gastroduce-japan.co.jp` domain gate | `app/auth/callback/route.ts`, `middleware.ts` |
| Calling the backend | `forward(request, path, {stream?})` adds the Bearer JWT | `lib/api.ts` |
| Calling the backend from a Server Component | `backendGet<T>()` | `lib/server-api.ts` |
| Chat UI, streaming, citations | assistant-ui + `runtime-provider.tsx` + `lib/sse.ts` | `components/` |
| Markdown rendering | `components/markdown.tsx` | |
| Citation chips | `components/citation-chip.tsx` | |
| Roles, folder ACLs, org admin | `app/org/`, `app/api/org/*` | |

**The browser never talks to Cloud Run directly.** Every call goes
browser → Next.js route handler → FastAPI. Follow that or auth breaks.

### Reference implementation to copy from

Flownote (`../flownote`) is the retired Electron predecessor. Its meeting history
UI works and is the agreed shape. Port it; do not redesign it.

```
../flownote/src/main-window/pages/history/
  index.tsx              layout
  SessionListView.tsx    list, grouped by date (127 LOC)
  SessionDetailView.tsx  tabs: summary / transcript / qaHistory (158 LOC)
  ChatBar.tsx            ask a question about this session (73 LOC)
  ChatAnswerModal.tsx    the answer (164 LOC)
```

Its data shapes are in `../flownote/src/types/global.d.ts:287-310`
(`SessionTranscript`, `SessionMessage`, `SessionQA`). Useful as a sanity check
on the model below.

---

## 4. Data model — proposed, needs your review

Migrations live in `migrations/`, numbered `NNN_name.sql`. **The latest is
`013_chatwork_source.sql`, so yours is `014_meetings.sql`.**

Follow the existing conventions exactly: RLS enabled with a
`USING (user_id = auth.uid())`-style policy as defence-in-depth, soft delete via
`deleted_at`, and FastAPI *also* filtering explicitly in SQL. See
`migrations/002_web_chat.sql` for the pattern.

```sql
-- one row per meeting the AI joined or is booked into
meetings
  id                uuid pk
  google_event_id   text unique      -- from the Calendar invite; the join key
  title             text
  meet_url          text
  scheduled_at      timestamptz      -- from the invite
  started_at        timestamptz null -- when the AI actually joined
  ended_at          timestamptz null
  status            text             -- scheduled|joining|live|ended|failed
  summary           text null        -- written after the meeting
  next_actions      jsonb null
  summary_status    text             -- pending|ready|failed
  agent_state       text             -- asleep|open  (see §6.4)
  created_at        timestamptz
  updated_at        timestamptz
  deleted_at        timestamptz null

-- one row per caption line, speaker-labelled
meeting_segments
  id                bigserial pk
  meeting_id        uuid fk -> meetings(id) on delete cascade
  seq               int              -- monotonic per meeting; dedupe key
  speaker           text             -- Meet's display name
  text              text
  spoken_at         timestamptz
  unique (meeting_id, seq)

-- who may see this meeting  (§5)
meeting_participants
  meeting_id        uuid fk
  email             text             -- lowercased; NOT user_id, see below
  is_organizer      bool
  added_by          text null        -- null = from the Calendar invite
  primary key (meeting_id, email)

-- ask-about-this-meeting reuses the chat stack (see below)
conversations
  + meeting_id      uuid null fk -> meetings(id)
```

**Participants are keyed by email, not `user_id`.** An invited colleague may
never have signed into Gastrobrain, so there is no `auth.users` row to point at.
Email is already the identity key on every surface (`ACCESS_CONTROL.md` §1), and
it is what the Calendar invite gives us.

### The one design call worth arguing about — decided

**Reuse `conversations`, but with the FK pointing the other way:
`conversations.meeting_id`, one thread per person per meeting.**

`/voice` already writes into `conversations` / `messages`
(`WEB_ARCHITECTURE.md` §7), so pointing at that stack buys streaming, citations,
feedback, history and the id-swap invariant for free. That much of the original
recommendation stands.

What does not: `meetings.conversation_id` — a single shared thread — cannot
work as the table stands. `conversations.user_id` is `NOT NULL` with RLS
`user_id = auth.uid()` (`002_web_chat.sql`), so exactly one participant could
read the shared thread. Making it shared means relaxing ownership and rewriting
the RLS policy on the table every existing chat depends on.

Flipping the FK avoids all of it. Ownership, RLS and the whole chat stack are
untouched; the Q&A tab shows the caller's own threads about this meeting. The
in-meeting Q&A is not lost either — the agent answers out loud, so it is already
in the transcript as captions.

A separate `meeting_questions` table was the third option: simpler to read, but
it re-implements four things that already work.

**Meeting Q&A evidence (2026-09-17):** Each turn loads the selected meeting's
title, summary, scheduled/recorded timestamps, computed recorded duration,
calendar invitees, speaker labels from all segments, and the transcript tail
(16,000 characters). Calendar invitees are not confirmed attendees; people
given access through sharing are excluded from the invitee evidence. Speaker
labels do not cover silent attendees. Duration measures AI join → recorded end,
not necessarily the entire meeting; missing timestamps remain unknown.

`meeting_qa.plan_meeting_search` uses the configured mini model to choose whether
external company evidence is needed. Meeting facts use only the selected record;
missing meeting facts never justify searching other meetings. Mixed questions
(e.g. whether a proposal follows company policy) get a company search query
resolved from the meeting and recent Q&A. Explicit comparisons may retrieve other
meetings. Retrieval keeps the caller's existing access scope. Failed or invalid
routing skips retrieval and tells generation to disclose unavailable company
evidence. This routing is model-based, with regression tests and synthetic live
checks; it does not guarantee perfect intent classification.

Meeting-specific system rules separate record facts from company evidence and
forbid using other meetings to fill gaps. The Q&A source chips show only sources
referenced by `[N]` in the answer, retaining original numbers across streaming
and reload. Stored retrieval candidates are retained for the existing citation
mapping; hiding a candidate chip does not validate the claim itself.

---

## 5. Who may see a meeting — decided 2026-09-07

**Calendar attendees only, plus whoever the UI shares it with.** Itsuki's call.
Implemented in `014_meetings.sql`; fail-closed, so an email that is not a
participant sees nothing.

The option table below is kept for the reasoning, not as an open question.

**Why this turned out cheap.** The spec assumed participant-based access needed
a fuzzy map from a Meet display name to a Supabase user. It does not: the
Calendar invite is how the AI gets into the meeting at all
(`../meetron/AGENTS.md` §5.1), so the event's attendee list *is* the participant
list, and attendee emails match Gastrobrain identities exactly. That is the
whole reason the expensive-looking option won. Its cost is one field added to
`POST /v1/meetings` (§6.2).

The options as they stood:

| Option | Means | Cost |
|---|---|---|
| **Participants only** ✅ | You see it if you were invited | Needs `meeting_participants` — but keyed on Calendar attendee emails, not Meet display names, so the identity mapping is exact |
| **Everyone in the company** | Any signed-in staff member reads any meeting | Trivial. But internal meetings include 1-on-1s, personnel talk, salary talk |
| **Folder-ACL style** | Meetings inherit the existing `docs/ACCESS_CONTROL.md` model | Consistent with the rest of Gastrobrain; more work up front |

Note the asymmetry that makes this urgent: Gastrobrain deliberately excludes HR,
経理 and 法務 material at ingestion time
(`config/notepm_excluded_notes.yaml`), so those topics are *not* in the corpus.
But a recorded internal meeting can contain all three, verbatim. **A meeting
transcript is more sensitive than the knowledge base it sits next to.** Defaulting
to company-wide would quietly undo an existing safeguard.

Shipped: **participants only, plus the person who invited the AI**, with a
visible "share with…" action. Never company-wide by default.

**Expect this to look like a bug the first time it happens:** a transcript can
name speakers who cannot read it — someone was forwarded the link, or pulled in
mid-meeting, and so is not on the invite. That is the safe direction to fail, and
"share with…" is the fix; it is not a permissions error.

---

## 6. The contract with the meeting agent

This is the only shared surface. Both sides code against it.

### 6.1 Authentication

The VPS is a machine, not a person, so it has no Supabase user JWT. It
authenticates to FastAPI with a **service token in a header**, checked against an
env var, and every `/v1/meetings/*` write path accepts it. Read paths used by
the browser keep the normal user JWT.

```
X-Meeting-Agent-Token: <shared secret>
```

Do not let the VPS write to Postgres directly. It is a separate machine on the
public internet; keep it behind the API where the checks already live.

### 6.2 The meeting agent calls you

| Method | Path | When | Body |
|---|---|---|---|
| `POST` | `/v1/meetings` | **On discovery**, then again on joining. Idempotent on `google_event_id`. | `{google_event_id, title, meet_url, scheduled_at, attendees: [{email, is_organizer?}]}` |
| `PATCH` | `/v1/meetings/{id}` | Status changes, and writing the `open` expiry back | `{status?, started_at?, agent_state?}` |
| `POST` | `/v1/meetings/{id}/segments` | Every few seconds, batched | `{segments: [{seq, speaker, text, spoken_at}]}` |
| `POST` | `/v1/meetings/{id}/end` | Leaving | `{ended_at}` — you generate the summary from here |
| `GET` | `/v1/meetings/{id}/state` | Polled every ~3 s | → `{agent_state: "asleep"\|"open"}` |

**Two things about `POST /v1/meetings` changed after §5 was settled** — both
are call-site changes on the meeting side, not new endpoints:

1. **`attendees` is required.** It is the access-control list (§5). The
   participant list is replaced wholesale on every post, except rows a human
   added through "share with…", which survive re-sync.
2. **The calendar watcher posts on discovery, not only on join.** Nothing else
   creates a `scheduled` row, and §7.1 shows upcoming meetings. The endpoint is
   already idempotent, so re-posting the same event on every poll is fine.

**`PATCH /v1/meetings/{id}` is shared by both callers** — the agent sends
`status`, `started_at` and `agent_state` with the service token, the browser
sends `title` with a user JWT. One handler, dual auth, each side allowed only
its own fields. The browser writes `agent_state` through `POST /{id}/state`
instead, so "who last wrote it" stays readable.

**Segments must be idempotent.** The VPS will retry on network failure and may
resend a batch. `unique (meeting_id, seq)` plus `ON CONFLICT DO NOTHING` handles
it. Do not assume batches arrive in order or exactly once.

### 6.3 You call nothing on the meeting agent

Deliberate. The VPS has no inbound port and no stable address. **All control flows
by the VPS polling `GET /v1/meetings/{id}/state`.** A ~3 s delay on a button press
is fine, and it saves a websocket, a callback URL and a firewall rule.

### 6.4 `agent_state` — two values, and what they mean

```
asleep  the AI listens and transcribes but never speaks   (default)
open    the AI answers without needing its name; expires after 90 s idle
```

The 90-second expiry is enforced **on the meeting side**, not by you. You store
the value and serve it. When the VPS times out it `PATCH`es the row back to
`asleep` — `agent_state` is accepted on `PATCH` with the service token for
exactly this. Do not implement your own timer; two timers on two machines will
disagree.

Without that write path the timeout cannot take effect at all, and it is not
obvious from either side alone: the gate closes locally, the next 3 s poll reads
the row still saying `open`, and the gate re-opens — every 3 s, forever, with the
UI showing a state the agent is not in. The meeting side additionally applies the
polled value **edge-triggered** (on change, not on every read), so a stale row can
never re-open a gate that closed locally.

Your UI writes this via `POST /api/meetings/{id}/state`.

---

## 7. What to build

### 7.1 Pages

```
/meetings            list. Group by date, newest first (copy SessionListView).
                     Show: title, date, duration, participant count, status.
                     A "live now" row if status = live, with the asleep/open
                     toggle inline — this is the only in-product control besides
                     Meet chat.

/meetings/[id]       detail. Three tabs, exactly Flownote's:
                       サマリー    summary + next actions
                       文字起こし  speaker-labelled transcript
                       Q&A        questions asked about this meeting
                     Editable title (Flownote does this; keep it).
                     A chat bar at the bottom → the conversation stack.
```

Add a Meetings entry to the existing nav. Match the current visual language —
this is a new room in the same house, not a new house.

### 7.2 Next.js route handlers

Thin proxies using `forward()`, exactly like `app/api/threads/route.ts`:

```
GET    /api/meetings              list
GET    /api/meetings/[id]         detail + segments
PATCH  /api/meetings/[id]         rename
POST   /api/meetings/[id]/state   asleep | open
DELETE /api/meetings/[id]         soft delete
```

### 7.3 FastAPI endpoints

In `src/gastrobrain/web_api.py`, following the existing handlers. Every query
filters explicitly on the caller's identity — do not rely on RLS alone; that is
defence-in-depth here, not the control.

### 7.4 Summary generation

On `POST /v1/meetings/{id}/end`, generate a Japanese summary and next actions
from the transcript. Reuse the existing LLM path in `src/gastrobrain/generate.py`
rather than adding a new client. Look at
`../flownote/supabase/migrations/20260331_auto_summary.sql` for the shape
Flownote settled on.

Slack delivery of that summary is **Stage C and not yours yet** — but write the
summary so it can be posted as-is.

---

## 8. Decisions already made — do not re-litigate

| Decision | Rationale |
|---|---|
| Lives in the existing Gastrobrain site, not a new app | The permission model that decides what the AI may read is already here. Two auth systems for one permission question is how permission bugs happen. |
| Never sold or distributed outside the company | Confirmed 2026-09-07. This is why a separate product site was rejected. |
| Internal meetings, not client 商談 | Changes what is sensitive (§5) and what is not (participant consent). |
| No browser extension | Control is Meet chat plus this site. |
| Two agent states, not three | `asleep` already does not speak; a separate "muzzled" adds only deafness to the wake word. |
| Port Flownote's history UI, don't redesign | It works and the shape is agreed. |

---

## 9. Running and verifying

```bash
cd web
npm install
npm run dev          # needs NEXT_PUBLIC_SUPABASE_*, GASTROBRAIN_API_URL
npx tsc --noEmit     # must be clean
npm run test         # node:test; currently 14 wake-word tests, all passing
npm run build        # next build must pass before you call anything done
```

Success criteria, per `CLAUDE.md` §4 — state them before you start, not after:

1. Migration applies cleanly and rolls back → verify with `supabase db reset`
2. A seeded fake meeting renders in `/meetings` and `/meetings/[id]` → verify by eye
3. `POST /v1/meetings/{id}/segments` twice with the same `seq` inserts once → verify with a test
4. A non-participant cannot read a meeting → verify with a test, once §5 is settled
5. `tsc --noEmit`, `npm run test` and `next build` all clean

### Built, 2026-09-07

| Piece | Where |
|---|---|
| Migration | `migrations/014_meetings.sql` — **applied to prod** (verified 2026-09-09) |
| Agent-facing API (§6.2) | `src/gastrobrain/web_api.py` — upsert / patch / segments / end / state |
| Browser-facing API (§7.3) | same file — list / detail / state / share / delete / summary |
| Summary generation (§7.4) | same file, `_summarize_meeting`; runs in the background after `/end` |
| Transcript in the chat prompt | `generate.py` + `pipeline.py` `extra_context`; see below |
| Next.js proxies (§7.2) | `web/src/app/api/meetings/**` |
| Pages (§7.1) | `web/src/app/meetings/**`, `web/src/components/meeting*.tsx` |
| Tests | `tests/test_meetings.py`, `web/src/lib/meetings.test.ts` |

Two things are worth knowing that the spec did not anticipate:

- **The transcript has to reach the model.** `answer_stream` refuses when
  retrieval returns nothing, which is the normal case for "what did we decide
  about X in this meeting". A thread with a `meeting_id` therefore carries the
  transcript (and the summary, once written) as `extra_context`, which also
  counts as evidence for the refusal check. Retrieval still runs and still
  cites the corpus; the transcript is quoted without `[N]` markers because it
  is not a citable document.
- **`renamed_at`** exists so the calendar watcher's next poll does not undo a
  human's rename.

### Still to do

1. ~~Apply `014_meetings.sql` to prod.~~ **Done.** Verified 2026-09-09 against
   project `zmbtkestevyojczqikrm`: all three tables present with RLS on,
   `conversations.meeting_id` present, `meetings_touch_updated_at` present, 3
   policies, 16 columns on `meetings` (so `renamed_at` is in). All at 0 rows.
   Note it does **not** appear in `supabase_migrations` — neither do 013 and
   several others, because this repo applies raw SQL rather than going through
   the Supabase CLI. Expected, not a gap.
2. ~~Set `MEETING_AGENT_TOKEN` and redeploy Cloud Run.~~ **Done.** `POST
   /v1/meetings` with no header returns 401; `require_meeting_agent` returns 503
   when the variable is empty and can only reach the 401 branch when it is set.
   So the token is live on the deployed revision.
3. ~~Confirm the two §6.2 call-site changes with the meeting-side agent.~~
   **Confirmed 2026-09-07**, along with a gap that is now fixed: `agent_state`
   had no service-token write path, which would have made the 90 s expiry a
   no-op (the VPS would re-open the gate on its next poll, every 3 s, forever).
4. Look at `/meetings` and `/meetings/[id]` with the seeded meeting below.
   Nothing is seeded yet, so both render empty until you run it.

### Seeding a meeting to look at

Once the migration is applied and the token is set, the agent-facing endpoints
are the seed script — this is exactly what the VPS will send:

```bash
API=https://gastrobrain-<hash>-an.a.run.app
TOK=$MEETING_AGENT_TOKEN

MID=$(curl -sX POST "$API/v1/meetings" -H "X-Meeting-Agent-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{
    "google_event_id": "seed_1", "title": "週次定例",
    "meet_url": "https://meet.google.com/abc-defg-hij",
    "scheduled_at": "2026-09-07T10:00:00+09:00",
    "attendees": [{"email": "itsuki.son@gastroduce-japan.co.jp", "is_organizer": true}]
  }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -sX PATCH "$API/v1/meetings/$MID" -H "X-Meeting-Agent-Token: $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"status": "live", "started_at": "2026-09-07T10:01:00+09:00"}'

curl -sX POST "$API/v1/meetings/$MID/segments" -H "X-Meeting-Agent-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{"segments": [
    {"seq": 1, "speaker": "田中", "text": "楽天のSKU上限を確認したい。", "spoken_at": "2026-09-07T10:02:00+09:00"},
    {"seq": 2, "speaker": "佐藤", "text": "先週の資料に載っています。", "spoken_at": "2026-09-07T10:02:20+09:00"}
  ]}'

# …look at /meetings while it is live, then end it and watch the summary land:
curl -sX POST "$API/v1/meetings/$MID/end" -H "X-Meeting-Agent-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{"ended_at": "2026-09-07T10:45:00+09:00"}'
```
