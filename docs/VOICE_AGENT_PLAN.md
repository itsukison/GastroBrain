# Voice agent — design & runbook (Layer 2a, OpenAI Realtime)

Status: **built, not yet deployed.** Written 2026-08-09, implemented the same
day. Supersedes the "What we'd have to build" section of
[`VOICE_AGENT.md`](VOICE_AGENT.md) §Layer 2a; the build-vs-buy analysis there
still stands.

Decision taken: **OpenAI Realtime API**, hosted inside the existing Next.js app
on Vercel.

**Before it can run, one thing is required:** set `OPENAI_API_KEY` in the
Vercel project env. Everything else is in the repo. See §12 for the deploy
checklist and §11 for what is verified vs. still untested.

---

## Summary

**Yes — build on the existing web app.** Auth (Slack OIDC → Supabase), the
Cloud Run proxy, per-user ACL, citation rendering, and thread persistence all
already exist and are exactly what a voice surface needs. Audio never touches
Vercel: WebRTC runs browser ↔ OpenAI directly, so no serverless timeout or
bandwidth limit applies to the media path. We add one page and two small JSON
routes.

**One change from `VOICE_AGENT.md`:** do *not* point the Realtime session at
our MCP server as a hosted MCP tool. Instead expose a **client-side function
tool** that calls our existing RAG pipeline through the existing Next.js proxy.
This is the "chat-supervisor" pattern from OpenAI's own reference app — the
realtime model does ears and mouth, our Sonnet pipeline does the answering.
It is better on the two things prioritised here (accuracy, ACL) and removes the
PAT-provisioning work entirely. Rationale in §2.

What shipped: 2 backend endpoints, 2 Next.js routes, 1 page, 1 client
component, 1 prompt module, 12 tests, 1 new npm dependency. No migrations, no
new infrastructure, no changes to Slack or MCP.

---

## 1. Architecture

```
┌──────────────────────────────┐
│ Browser  /voice              │
│  @openai/agents-realtime     │◄══ WebRTC audio (direct, ~800ms round trip) ══►┌──────────────┐
│  RealtimeSession             │                                                │ OpenAI       │
│   └ tool: ask_gastrobrain ───┼──┐                                             │ gpt-realtime │
│  transcript + citation chips │  │                                             └──────────────┘
└──────────────────────────────┘  │ tool call executes IN THE BROWSER
              │                   │
              │ POST /api/voice/session   (mint ephemeral key)
              │ POST /api/voice/ask       (the tool body)
              ▼                   ▼
┌──────────────────────────────────────────┐
│ Next.js route handlers (Vercel)          │
│  - reads Supabase session from cookies   │
│  - /session → OpenAI client_secrets      │
│  - /ask     → forward() with Bearer JWT  │
└──────────────────────────────────────────┘
              │ Bearer Supabase JWT
              ▼
┌──────────────────────────────────────────┐
│ Cloud Run  POST /v1/voice/ask            │
│  resolve_access(email) → AccessScope     │
│  run_pipeline(surface="voice")           │
│  → {answer, citations, message_id}       │
└──────────────────────────────────────────┘
```

The only new trust boundary is browser ↔ OpenAI. Everything below the Next.js
line is the path `/api/chat` already takes.

---

## 2. Why a function tool instead of hosted MCP

Both work. The trade is where the *answering* happens.

| | Hosted MCP (`type: "mcp"`) | Function tool → `/v1/voice/ask` (**chosen**) |
|---|---|---|
| Who composes the answer | `gpt-realtime` reads 8 raw JP chunks live | `claude-sonnet-4-6` — the same pipeline as web/Slack |
| Citation / refusal / injection rules | Re-implemented in the voice prompt | Inherited from `_BASE_RULES` unchanged |
| Per-user ACL | Needs a per-user PAT injected into the session and **sent to OpenAI**; `mcp_tokens` has no expiry column | Supabase JWT over the existing proxy; nothing new leaves our infra |
| What OpenAI receives | Every retrieved chunk, verbatim | The question + the final ~150-char answer |
| Citations on screen | Not available client-side | Returned with the answer, rendered with `citation-chip.tsx` |
| Persistence / telemetry | Only `queries` via the MCP telemetry hook | Full `messages` + `queries` rows; voice threads appear in the sidebar |
| Latency per turn | ~1.0–1.5 s | ~2.5–4.0 s (mitigations in §6) |
| Code to write | Session config only | +1 backend endpoint, +1 Next route |

Hosted MCP is faster and less code. It loses on accuracy — a speech-to-speech
model synthesising from eight Japanese chunks mid-conversation is materially
weaker at grounding, numbers, and refusal than the Sonnet path we already
tuned — and it forces a long-lived per-user credential into OpenAI's session
config. Accuracy was the stated priority, so we pay ~1.5 s and buy it back
in §6.

**Note this does not retire the MCP server.** `/mcp/` stays exactly as it is
for Claude Code / Cursor / claude.ai. If we later want `query_sales` by voice,
the hosted-MCP path can be added alongside the function tool in the same
session (§10).

---

## 3. Backend

**`POST /v1/voice/ask`** (`web_api.py`) — a non-streaming twin of `chat`.
Collects the pipeline's `AnswerToken` events into one string instead of
emitting SSE, and passes `surface="voice"`.

```python
class VoiceAskBody(BaseModel):
    conversation_id: UUID
    question: str = Field(min_length=1, max_length=1000)  # a transcript, not an essay

class VoiceAnswerOut(BaseModel):
    answer: str                 # spoken text — no markdown, no [N] markers
    citations: list[dict]       # [{n, doc_title, doc_url, ...}] for the screen
    message_id: UUID
    query_id: UUID | None
    latency_ms: int
```

**`GET /v1/voice/vocab`** — the caller-visible proper nouns (§5.3). Scoped to
the user, so the hint list can't reveal a document they can't read. Cached 6 h
per access scope.

`chat`'s inline `_prep` was lifted to a module-level `_prep_turn(...)` and is
now shared by both endpoints — thread-ownership check, `AccessScope`
resolution, history window, and the user-turn insert happen in exactly one
place, so the two surfaces cannot drift apart on ACL.

Changes in `generate.py`:

1. `Surface = Literal["slack", "web", "voice"]`.
2. `_SURFACE_FORMAT` dict replaces the `slack`/else ternary in `system_prompt`.
3. New `_VOICE_FORMAT` block (spoken output rules) and `_MAX_TOKENS`, which
   caps voice at 400 tokens against 1024 for web/Slack.
4. `_no_chunks_message(surface)` — the voice refusal is
   「その件は資料に見当たりませんでした。」, deliberately different from the web
   wording so voice refusals are greppable in `queries` during eval.

`retrieve.py` gained `access_sql(scope) -> (fragment, params)`, the ACL
predicate the vocab query needs; `retrieve_candidates` now uses it too, so
there is one definition rather than two.

---

## 4. Next.js routes

### `POST /api/voice/session` — mint the ephemeral key

`app/api/voice/session/route.ts`. Guards with `requireUser("/voice")`, fetches
the user's vocabulary from `/v1/voice/vocab`, then mints a client secret:

```ts
await fetch("https://api.openai.com/v1/realtime/client_secrets", {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.OPENAI_API_KEY}`, ... },
  body: JSON.stringify({
    expires_after: { anchor: "created_at", seconds: 600 },
    session: { type: "realtime", model: VOICE_MODEL },
  }),
});
```

It returns `{ clientSecret, model, instructions, transcriptionHint }`. The
session *config* (turn detection, voice, transcription) is applied browser-side
by the Agents SDK on connect — the SDK sends its own `session.update` after the
handshake, so configuring it twice would just mean two places to keep in sync.
Nothing in that payload is a security boundary; the ACL lives in
`/v1/voice/ask`. `OPENAI_API_KEY` never leaves the server.

The vocabulary fetch is best-effort: if the backend is slow or the BigQuery
catalog is down, the session still starts with an empty hint list.

### `POST /api/voice/ask` — the tool body

Three lines, reusing the existing `forward()` helper, so the auth path is
byte-for-byte the one `/api/chat` uses.

```ts
export const runtime = "nodejs";
export const maxDuration = 60;
export async function POST(request: Request) {
  return forward(request, "/v1/voice/ask");
}
```

---

## 5. The `/voice` page

`app/voice/page.tsx` (server guard) + `components/voice-session.tsx` (client).
One new dependency: `@openai/agents-realtime` 0.14.3. No zod — the tool's
parameters are declared as plain JSON Schema, which the SDK accepts, so we
don't inherit its zod version constraint.

The tool's `parameters` are JSON Schema with `strict: true`; `execute` runs
**in the browser**, which is what lets it carry the user's cookie-backed
session with zero extra credentials:

```ts
execute: async (input) => {
  const { question } = input as { question: string };
  const resp = await fetch("/api/voice/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationRef.current, question }),
  });
  const data = (await resp.json()) as VoiceAnswer;
  setCitations(data.citations);   // rendered under the transcript
  return data.answer;             // the ONLY thing the model may speak
}
```

On failure it returns a Japanese sentence rather than throwing, because the
return value is read aloud verbatim — an English stack trace in the user's ear
is the worst possible error surface.

Session config applied on connect:

```ts
audio: {
  input: {
    transcription: { model: "gpt-4o-mini-transcribe", language: "ja", prompt: hint },
    turnDetection: { type: "semantic_vad", eagerness: "auto",
                     interruptResponse: true, createResponse: true },
    noiseReduction: { type: "near_field" },
  },
  output: { voice: "marin", speed: 1.0 },
}
```

UI behaviour:

- Mic button starts the session. Connect is user-gesture-triggered on purpose —
  it satisfies the browser's mic-permission and audio-autoplay requirements in
  one click, which "warm connect on page load" would not.
- Live transcript from `history_updated`; a 「社内資料を検索しています…」 row
  while `agent_tool_start`/`agent_tool_end` are outstanding.
- `citation-chip.tsx` reused verbatim — you cannot hear a citation, so the
  screen carries the audit trail.
- 「チャットで続ける」 → `/c/<conversation_id>`. Voice turns are written to the
  same `messages` table, so the thread is already there.
- A 55-minute auto-close with a countdown from 45, ahead of OpenAI's 60-minute
  hard cap, so the session ends cleanly instead of the transport dropping
  mid-sentence. `session.close()` also runs on unmount — otherwise navigating
  away keeps billing until OpenAI times the session out.

The conversation row is minted **when the user starts talking**, not on page
load, so opening `/voice` and walking away doesn't litter the sidebar. After
the first answer the existing `/api/threads/[id]/title` endpoint names the
thread, so voice sessions are findable later.

Entry point: a mic icon in the sidebar footer, next to 設定 and アクセスできる資料.

### 5.1 System prompt

Lives in `web/src/lib/voice-prompt.ts` — that file is the source of truth; this
section explains why it is shaped the way it is rather than restating it.

It follows OpenAI's realtime prompting guide skeleton: labelled single-topic
sections (Role / Personality & Tone / Language / Tools / Instructions /
Conversation Flow / Safety / Reference Pronunciations), sample phrases (the
model copies these closely), and an explicit language lock.

The load-bearing rules, in rough order of how much accuracy they buy:

1. **Never answer a factual question without calling the tool.** Without this
   the model happily answers from pretraining, confidently and wrongly.
2. **Read the tool result verbatim — no summarising, no rounding.** The
   supervisor already wrote a 150-character answer; letting the voice model
   "improve" it is exactly where numbers drift.
3. **Say a one-line preamble before calling the tool**, varied each time. This
   is the latency fix (§6) and the single biggest driver of how natural the
   thing feels.
4. **Expand pronouns into a self-contained `question` argument.** The Haiku
   rewriter is a backstop, not the primary mechanism.
5. **Refusal passthrough** — when the supervisor says 資料に見当たりません, the
   model says that and stops.

Note the prompt governs *conversation*, not *content*. Citation, refusal, and
prompt-injection defence are enforced in `generate.py::_BASE_RULES` on the
supervisor side, where they apply to every surface at once.

### 5.2 Reference pronunciations

A short list (RMS, CVR, ROAS, SKU, LTV, EC, 楽天) so the model *says* domain
terms the way the office says them rather than spelling out initialisms or
reading 楽天 as "Rakuten".

### 5.3 Domain vocabulary for ASR

The highest-leverage accuracy item, because a misheard proper noun becomes a
failed retrieval — and a failed retrieval is indistinguishable, to the user,
from "the company has no document about this."

`GET /v1/voice/vocab` assembles, per user:

- store IDs from `sales_schema()["catalogs"]["store_ids"]` — Japanese strings
  like 福栄組合, exactly what a general-purpose ASR mangles,
- `ec_platform` values,
- up to 120 recently-updated NotePM document titles **the caller can see**.

Titles longer than 40 characters are dropped (they're sentences, not nouns, and
they dilute the hint). The list feeds two places: the transcription model's
`prompt` (fixes recognition) and the tail of the system prompt (fixes
pronunciation). Cached 6 h per access scope.

> **`transcription.prompt` is hard-capped at 1024 characters.** Exceeding it
> does not degrade or truncate — it fails the entire session with
> `string_above_max_length`, after a successful connect, so it looks like a
> transport bug. `transcriptionHint()` budgets whole terms against that limit
> and never truncates mid-term: half a proper noun biases the transcriber
> toward a word that does not exist, which is worse than omitting it. With 73
> live terms the hint lands at 1003 chars / 48 terms kept. Pass terms
> most-valuable-first — the backend already puts short store names ahead of
> long document titles.

The ACL scoping here is deliberate: an unscoped hint list would leak the
*existence* of document titles a user cannot open.

---

## 6. Latency budget

Measured expectations, per question:

| Stage | Est. |
|---|---|
| Endpointing (`semantic_vad`) | 200–500 ms |
| Tool-call decision + args | 300–600 ms |
| Haiku rewrite (only with history) | ~400 ms |
| Retrieval: Cohere embed + pgvector + pgroonga + rerank | 600–900 ms |
| Sonnet generation, ≤400 tokens | 1,200–2,500 ms |
| Realtime first audio after tool return | ~300 ms |
| **Total dead air, unmitigated** | **3.0–5.2 s** |

### Measured, 2026-08-09

Three questions through `run_pipeline(surface="voice")` against the live
corpus, run **from a laptop** (Tokyo Supabase + Cohere over residential
network), so these are an upper bound — Cloud Run in `asia-northeast1` sits
much closer to both:

| Case | `latency_ms` | Notes |
|---|---|---|
| Retrieval only, 0 chunks → refusal | 1,329 / 2,384 | no LLM call at all |
| Full answer, 2 chunks, 144 chars | 4,250 | within the 150-char target |

So the supervisor turn is ~2.4 s of retrieval + ~1.9 s of generation on a
laptop. Re-measure from Cloud Run before tuning anything — that number, not
this one, is what users experience.

Mitigations, in order of value:

1. **Preamble before the tool call** (prompt rule §5.1). ~1.5 s of speech
   overlaps the wait. This is most of the problem, and it is already in the
   prompt.
2. **Cap the spoken answer** at 3 sentences / 150 chars — `_MAX_TOKENS["voice"]
   = 400` against 1024 for web. Verified: the measured answer was 144 chars.
3. **Haiku fast path.** `claude-haiku-4-5` grounded on the same reranked chunks
   is ~2× faster. Not implemented — add a `voice_generation_model` setting and
   A/B it against the eval set, switching only if accuracy holds.
4. **Retrieval is the bigger half.** If Cloud Run measurements still show
   >1.5 s, the win is in the Cohere round trips, not in the LLM.

Target: **perceived** silence under 2 s p50. `latency_ms` is already written to
`queries` for every voice turn, so this is measurable without new plumbing.

---

## 7. Natural conversation

- `semantic_vad` over `server_vad`: endpoints on meaning, not on a silence
  timer, so it stops cutting people off mid-thought when they pause to think.
  Japanese speakers pause a lot; this matters more here than in English.
- `interrupt_response: true` for barge-in — the user can talk over a long
  answer and the model stops.
- `gpt-realtime-2.1-mini` for the voice layer. It only does conversation and
  tool dispatch — the reasoning is Sonnet's job — so the flagship's extra
  capability buys little here at 3× the price. Validate Japanese prosody in the
  Phase 0 spike; fall back to `gpt-realtime-2.1` if mini's JP is noticeably
  worse.
- Variety rule in the prompt so the preamble isn't the same phrase every turn —
  identical fillers are the fastest way to make an agent feel robotic.
- 60-minute hard session cap on OpenAI's side. The UI counts down from 45 min
  and closes at 55, so the session ends on our terms.
- `noiseReduction: near_field` — these are laptop mics in an open office.

---

## 8. Cost

| | Per month @ 2,000 min |
|---|---|
| `gpt-realtime-2.1-mini` audio ($10/$20 per 1M) | ¥6,000–12,000 |
| Sonnet supervisor (~1,300 questions × ~¥3) | ~¥4,000 |
| Cohere embed + rerank | ~¥400 |
| **Total** | **~¥10,000–16,000/月** |

Flagship `gpt-realtime-2.1` instead of mini: ¥18,000–33,000 + the same ~¥4,400.
**【推測】** ¥150/$; the listening/speaking mix moves this ±40%.

---

## 9. Privacy

What reaches OpenAI: the user's audio, its transcript, the question sent to the
tool, and the final ~150-char answer. **Not** the retrieved chunks — that is a
concrete improvement over the hosted-MCP design and narrows the disclosure
compared with `VOICE_AGENT.md` §Recommended next steps #1.

Decision still required from the boss: OpenAI joins Anthropic and Cohere as a
data processor. Check whether our API org has zero-data-retention enabled
before the first real session.

---

## 10. Explicitly out of scope for v1

- **`query_sales` by voice.** 60 s timeout; a minute of silence is unusable.
  When we do it: hosted MCP alongside the function tool, plus a "結果はチャットに
  出しました" pattern instead of reading numbers aloud.
- **Voice cloning.** Requires Layer 2b — separate decision (`VOICE_AGENT.md`).
- **Phone (SIP) access.** Realtime supports it; no demand yet.
- **Slack huddle integration.**

---

## 11. Status — verified vs. untested

**Verified (2026-08-09):**

- `run_pipeline(surface="voice")` against the live corpus: spoken answers carry
  no markdown, no `[N]`, no URLs; 144 chars on a real question; the refusal
  path fires correctly on an invented topic. Latency in §6.
- `pytest` — 29 pass, including 12 new voice tests (format selection, token
  cap, refusal wording, ACL fragment, request validation).
- `tsc --noEmit` clean; `next build` succeeds; `/voice` is a 219 kB route, and
  the SDK is loaded only there.
- Lint: no new rule classes versus `main`.

**Not yet tested — needs the API key:**

- Anything through OpenAI. No Realtime session has ever been opened, so the
  Realtime session-config field paths, the WebRTC handshake, tool dispatch,
  Japanese prosody, and the transcription hint are all unexercised.
- The `client_secrets` payload shape. **【推測】** the `expires_after` /
  `session` shape is right per the current docs, but a mismatch will surface as
  a 400 from that endpoint on the very first click — loud, not silent.
- Whether `gpt-realtime-2.1-mini`'s Japanese is good enough, or whether we need
  the flagship. One config change (`OPENAI_VOICE_MODEL`) either way.

**Remaining work, in order:**

| # | Work | Verify by |
|---|---|---|
| 1 | Set `OPENAI_API_KEY` in Vercel; confirm ZDR status on the OpenAI org | A session connects and the agent greets in Japanese |
| 2 | Model choice: mini vs. flagship | A human holds a 3-minute Japanese conversation on each and picks |
| 3 | Prompt iteration against a 20-question script | Tool-call rate 100 % on factual questions; 0 unsourced factual claims |
| 4 | Latency re-measurement from Cloud Run; Haiku A/B if needed | p50 perceived silence < 2 s |
| 5 | Eval: the *same questions* as the text eval (`docs/PRD.md`) | Voice answers agree with web answers; ASR correct on 10 store names |

Step 5 is the payoff of this architecture: both surfaces run one pipeline, so
any divergence in answer *content* is a bug in the voice format block, never a
retrieval regression.

---

## 12. Deploy checklist

**Vercel** (the only new configuration anywhere):

- `OPENAI_API_KEY` — server-side only. Do **not** prefix `NEXT_PUBLIC_`.
- `OPENAI_VOICE_MODEL` — optional; defaults to `gpt-realtime-2.1-mini`.

**Cloud Run** — no new env vars. `/v1/voice/ask` and `/v1/voice/vocab` ship
with the existing image; redeploy as usual.

**Supabase** — no migrations. Voice writes to the existing `conversations` /
`messages` / `queries` tables.

**Rollback** — remove `OPENAI_API_KEY`. `/api/voice/session` then returns 500,
the page shows an error, and nothing else is affected. To hide the entry point
as well, remove the mic `<Link>` from `thread-sidebar.tsx`.

**Watch after release** — voice rows in `queries` (`latency_ms` distribution,
and how often the answer equals the voice refusal string, which is the "we
searched and found nothing" rate).

---

## Sources

- [OpenAI — Realtime with tools (MCP)](https://developers.openai.com/api/docs/guides/realtime-mcp)
- [OpenAI — Realtime and audio guide](https://developers.openai.com/api/docs/guides/realtime)
- [OpenAI — Voice agents guide](https://developers.openai.com/api/docs/guides/voice-agents)
- [OpenAI Cookbook — Realtime Prompting Guide](https://developers.openai.com/cookbook/examples/realtime_prompting_guide)
- [OpenAI — Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- [OpenAI Agents SDK (JS) — Realtime agents](https://openai.github.io/openai-agents-js/guides/voice-agents/)
- [openai/openai-realtime-agents — chat-supervisor pattern](https://github.com/openai/openai-realtime-agents)
- [OpenAI — Updates for developers building with voice](https://developers.openai.com/blog/updates-audio-models)
- [OpenAI Realtime API pricing, 4,000 measured sessions](https://hackernoon.com/openai-realtime-api-pricing-in-2026-real-world-data-from-4000-measured-sessions)
- [Realtime API production guide 2026 (latency targets)](https://www.forasoft.com/blog/article/openai-realtime-api-voice-agent-production-guide-2026)
- [Realtime session length limits](https://community.openai.com/t/realtime-api-hows-everyone-managing-longer-than-30min-sessions/1144295)
