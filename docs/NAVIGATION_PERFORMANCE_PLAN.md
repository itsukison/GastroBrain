# Web navigation performance plan

Date: 2026-09-17. Code reviewed: `fff1ac3`.
Target: `https://gastron-brain-web.vercel.app/meetings` and its connected routes.
Status: first implementation stage complete locally; deployment and production
measurement pending. See the implementation record below. The investigation
sections preserve the original findings and proposed follow-up work.

## Implementation record

Implemented against `c8b3d58`, including the committed inline meeting Q&A UI:

- Route loading boundaries for meetings list/detail, chat, voice, access and
  root entry; semantic navigation links with pending feedback.
- Independent sidebar streaming and immediate new-chat button feedback.
- Request-scoped verified session shared between page guards and backend calls.
- In-memory return-to-chat destination to bypass `/` after visiting a chat;
  no prefetch for the `/` fallback, which can lead to thread creation.
- Paused/cancellable, non-overlapping meeting polling; list/sidebar state
  reconciliation on refreshed server props; detail keyed by meeting ID.
- 15-second normal backend-read deadline and retryable route error UI, with
  actual missing/unauthorized meetings still mapped to 404.

Production fixture verification found that `web/middleware.ts` was not included
by a fresh production build with `src/app`. Moved it to `web/src/middleware.ts`.
The fixture now confirms login redirects and exactly two Auth user requests per
page request: middleware plus the shared Server Component verification. Chat's
layout, sidebar and page share the latter too. Development artifacts are not
proof of production middleware inclusion.

Validation: 42 unit tests passed (including verified-session cases), typecheck
and production build passed. `web/scripts/check-navigation.mjs` creates a
temporary production build with synthetic local auth and backend services. It
passed streaming order, shared auth, concurrent user isolation, unauthenticated
redirects, missing-vs-unavailable meeting handling and read-only navigation.

Observed fixture timings (not production benchmarks): list skeleton 52 ms,
list data 596 ms; detail skeleton 16 ms, detail data 626 ms; chat messages
584 ms, deliberately slower sidebar 994 ms. Backend fixtures delay regular
reads by 500 ms and sidebar reads by 900 ms. No browser UI automation was used;
client click timing, rendering/layout and real login/token rotation still need
validation in the deployed environment.

Remaining after this stage: production traces and deployment-region checks,
lightweight meeting detail/transcript pagination, incremental live transcript
reads, optional client query caching and JWT-key-cache-miss tuning. These are
not prerequisites for the implemented streaming/navigation changes. Review
the measurements before broadening the backend/API changes.

## Evidence and limits

Read `../flownote/AGENTS.md`, the web architecture and meetings specifications,
and the actual web/backend implementation. This website lives in `gastro/web`,
not `flownote/flownoteweb`. Installed and locked Next.js version is 15.5.18.

Findings below are code-confirmed behavior, not measured production timings.
No authenticated production trace was captured and deployed commit parity has
not been verified. Computer use was stopped at the user's request. Actual
latency contributions, deployment regions and response sizes still need measurement.

Concurrent uncommitted edits to the meeting detail/Q&A/share UI appeared during
the review and were left untouched. Findings refer to the reviewed baseline;
reconcile those edits before implementation. In particular, the new Q&A work
removes its old `router.refresh()` and adds a message-history fetch on mount,
which should be considered when deciding what to defer until tab selection.

## Current architecture and navigation

```text
Client navigation (Link / router.push)
  -> Next.js middleware: Supabase getUser / session refresh
  -> server page: requireUser
  -> backendGet -> backend: getUser, then getSession for the bearer token
  -> Cloud Run FastAPI: JWT verification and authorization
  -> Supabase Postgres
  -> React Server Component payload -> destination rendered

Client refresh/mutation
  -> same-origin /api/* -> middleware -> backend/forward -> Cloud Run
```

The meetings pages await authentication and all their initial data before
returning their view. They are `force-dynamic`; backend reads use `no-store`.
No `loading.tsx`, explicit Suspense boundary, or route `error.tsx` exists in
the app. Meeting links already use Next Link; navigation is not implemented
as a full document reload. All sections share one root layout, but only chat
has a persistent section layout/sidebar. Meetings and voice intentionally
use their own full-width views.

The summary/transcript/Q&A buttons inside a meeting are local React state
changes, not navigations or network requests. If those are slow, investigate
transcript rendering and main-thread work separately from route latency.

## Findings, in priority order

| Finding | Evidence | Consequence |
| --- | --- | --- |
| No streaming loading boundaries | `web/src/app/meetings/page.tsx`, `meetings/[id]/page.tsx`, and the app file tree | The old screen remains while the destination waits; dynamic links lack the normal partial-prefetch loading boundary. |
| Repeated authentication work | `lib/supabase/middleware.ts`, `lib/auth-guard.ts`, `lib/api.ts` | A meetings request invokes `getUser` in three places. Middleware is a separate execution boundary; measure outbound calls rather than assuming every invocation is a distinct HTTP request. There is no explicit shared request auth context. |
| Return-to-chat detour | `components/meetings-view.tsx:47`, `app/page.tsx`, `app/(chat)/layout.tsx` | Back goes to `/`, which fetches one thread and redirects; the destination then needs the sidebar and messages. |
| Chat layout blocks its entire shell | `app/(chat)/layout.tsx` | Entering chat waits for authentication and the thread list before its sidebar/shell can render. A loading file below this layout alone cannot cover its own awaits. |
| Full transcript is required for summary display | `src/gastrobrain/web_api.py:get_meeting`, `components/meeting-detail.tsx` | Four sequential SQL statements fetch metadata, participants, all segments, and own Q&A threads. Unbounded segments are sent even when the summary tab is selected. |
| Polling repeats the full detail request | `components/meeting-detail.tsx:49-65` | Every 10 seconds while live/summary-pending, all segments are fetched again. Polling continues in hidden tabs and can overlap if responses are slow. |
| Chat rows are imperative clicks | `components/thread-sidebar.tsx` | Clickable divs call `router.push` without Link prefetch behavior or navigation-pending feedback. |
| Failed reads look like legitimate empty/missing data | meetings pages and chat layout | List failures become an empty list; every detail error becomes 404. No explicit read deadline exists in `backendGet`. |

Secondary correctness issue relevant to refresh: meetings views and the sidebar
initialize `useState(initial)` without reconciling subsequent `initial` props.
`router.refresh()` can repeat server work without updating those local arrays.
Meeting Q&A calls it after an answer. Fix this alongside targeted invalidation;
do not overwrite active edits or streaming state with a late response.

## Implementation sequence

### 1. Establish a small baseline

Measure list -> detail, detail -> list, meetings -> chat, chat -> meetings,
and chat -> another thread. Include first visits, repeat visits and back/forward.
Use a production build: development mode is not a valid prefetch benchmark.

Record click -> pending feedback, click -> destination shell, and click -> useful
data separately. Add server spans for middleware auth, render auth, upstream
fetch, DB pool acquisition, individual queries and response bytes. Use request
IDs and durations without logging tokens or meeting text. Confirm deployed
revision and Vercel/Cloud Run/DB regions before attributing network costs.

### 2. Make navigation acknowledge clicks and stream useful structure

- Add route-specific `loading.tsx` for meetings list and detail, plus chat,
  voice and access pages. Add a root fallback to cover entry into asynchronous
  section layouts. Avoid putting new blocking work above these boundaries.
- Match the destination layout: list header + row placeholders; detail header,
  tabs and summary placeholders; chat sidebar/message placeholders. Include an
  accessible loading status and respect reduced motion. Do not display fake
  meeting text or active mutation controls before data/authorization resolves.
- Add pending feedback at the clicked link using Next 15.5's supported link
  status API, and transition feedback for imperative navigation. This covers
  the period before an uncached fallback arrives, including middleware latency.
- Convert chat row navigation to semantic Links, with delete as a separate
  sibling button. Use default partial prefetch first; consider bounded
  hover/focus prefetch only if measurements justify it.
- Stream the chat sidebar data behind its own Suspense boundary so it does not
  hold back the entire chat shell. Keep full-width meetings/voice layouts.
- Do not blanket-prefetch all meeting details: each currently includes the full
  transcript. Do not prefetch `/new`: rendering it currently creates a thread.

Skeletons reduce the silent wait, not the time required to fetch the data.
They must enclose the slow server work to help. A spinner inside the client
view that only mounts after its server page resolves would be too late.

### 3. Remove redundant work from the navigation path

- Introduce one request-scoped server auth/session helper shared by
  `requireUser` and `backend`; use React request memoization for server renders,
  not a process-global user/token cache. Route handlers need explicit reuse
  within their own request. Middleware cannot share that React cache.
- Preserve cookie rotation and the documented first-login hydration behavior.
  Keep backend JWT and meeting participant/owner authorization intact.
- Evaluate `getClaims` for middleware only after confirming signing-key type
  and desired revocation behavior. Cached asymmetric verification can avoid
  an Auth round trip; symmetric signing still requires a server call. Do not
  replace verified auth with trusting `getSession` alone.
- Remember the previous valid chat route in a lightweight shared client
  navigation context and return directly to it. Retain `/` as the safe fallback
  for a fresh deep link; do not use unconditional browser back.
- Keep mutations behind POST/explicit actions. If `/new` is redesigned later,
  make its GET render a draft composer and create a thread on explicit action.

### 4. Reduce detail payloads and preserve already-loaded data

- Add a lightweight detail read for summary/metadata/participants/thread
  summaries, and an authorized cursor-based transcript endpoint. Keep existing
  endpoint behavior compatible with other callers during migration.
- Load transcript on tab intent/selection, paginate it, and append new segments
  using `after_seq`. Virtualize only if large-transcript render timings justify it.
- Poll status/summary separately from transcript; stop when hidden or complete,
  avoid overlapping requests and cancel obsolete requests on navigation.
- Introduce a small user-scoped client query cache if repeat navigation remains
  slow. TanStack Query is installed but unused. Seed it from server data, clear
  it on account change/logout, and invalidate affected entries after writes.
  Cached data must be rendered inside a non-blocking route shell; a client cache
  alone cannot accelerate a page that still awaits an unconditional server fetch.
- Replace broad `router.refresh()` calls with updates/invalidation of affected
  meeting/thread data. Ensure a new Q&A thread appears immediately.

### 5. Make waits bounded and tune infrastructure from evidence

- Add a finite deadline for normal backend reads and retryable error states;
  preserve 404 for missing/unauthorized meetings. Distinguish 401, 404, upstream
  failure and timeout. Do not apply a short read timeout to chat/voice SSE.
- Check Vercel compute placement relative to Cloud Run and Supabase. The deploy
  script specifies Tokyo and one minimum Cloud Run instance; deployed values
  are unknown, so scale-to-zero is not an established explanation.
- Check database query plans and pool wait before changing indexes. Existing
  meeting time, participant email and conversation meeting indexes already exist.
- Backend JWT keys are cached for an hour, but a cache miss performs synchronous
  HTTP inside an async auth dependency. Investigate its event-loop impact if
  tracing shows occasional correlated stalls; do not assume it explains every click.

## Acceptance criteria

Proposed targets, not measured results: pending feedback within 100 ms after
hydration; prefetched destination shell within 200 ms on the agreed test
network; report p50/p95 useful-data time separately and show improvement against
the baseline. Cold/uncached loading must also provide prompt click feedback.

Validate rapid repeated clicks, slow/failed backend responses, long transcripts,
back/forward, first Slack login, expired-token refresh, logout/account switching,
and direct detail links. No extra thread should be created by prefetch. Users
must still be unable to read another user's chat or an uninvited meeting. Check
that Q&A and sidebar refreshes update visible data and no stale response replaces
the current meeting. Run web tests, typecheck and production build; add focused
tests for changed auth behavior, API contracts and meaningful cache regressions.

Ship loading/pending UX and request-auth deduplication first, then compare
measurements before undertaking the API/cache changes.

## Framework references

- [Next.js 15 prefetching](https://nextjs.org/docs/15/app/guides/prefetching)
- [Next.js 15 loading boundaries](https://nextjs.org/docs/15/app/api-reference/file-conventions/loading)
- [Supabase SSR and auth-method tradeoffs](https://supabase.com/docs/guides/auth/server-side/creating-a-client?framework=nextjs)
