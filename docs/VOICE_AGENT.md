# Voice agent — options for talking to Gastrobrain

Status: **research, decided.** Written 2026-08-05 to support a build-vs-buy
decision. Layer 2a was chosen and built on 2026-08-09 — see
[`VOICE_AGENT_PLAN.md`](VOICE_AGENT_PLAN.md) for what was actually implemented
(notably: a client-side function tool, not the hosted-MCP wiring sketched in
§Layer 2a below). This document is kept for the comparison.

The goal: let people ask Gastrobrain questions by speaking, instead of typing
into Slack or the web app. Gastrobrain already exposes its retrieval pipeline
over MCP (`docs/MCP.md`), so the question is which voice surface can call that
MCP server — not whether to rebuild retrieval.

---

## Summary

| | Layer 1 — subscription voice | Layer 2a — OpenAI Realtime | Layer 2b — ElevenLabs Agents | Layer 3 — self-built |
|---|---|---|---|---|
| **Works today** | ❌ No | ✅ Yes | ✅ Yes | ✅ Yes |
| **Cloned voice** | ❌ | ❌ preset voices only | ✅ | ✅ |
| **Per-user NotePM ACL** | — | ✅ | ⚠️ unresolved | ✅ |
| **Cost @ 2,000 min/月** | ¥0 | ¥6,000–33,000 | ¥15,000–45,000 | ¥5,000–10,000 + eng. time |
| **Build effort** | — | 2–4 days | 2–3 days + recording | 3–6 weeks + upkeep |

**Recommendation: Layer 2a (OpenAI Realtime)** unless the cloned voice is a
hard requirement, in which case Layer 2b — but only after resolving the ACL
question below. Layer 3 is not needed for voice cloning.

---

## Layer 1 — use the voice mode of a subscription we already pay for

**The idea.** We already connect Gastrobrain's MCP server to ChatGPT, Claude,
and Gemini as a custom connector for text chat. If their voice modes used the
same connectors, voice would cost us ¥0 in marginal spend.

**Verdict: this does not exist on any platform as of August 2026.** The
connector works in text, then silently stops working when you switch to voice.

| Platform | Text MCP | Voice MCP | Evidence |
|---|---|---|---|
| ChatGPT (Plus/Pro, Developer Mode) | ✅ | ❌ | Community thread opened 2026-05-30, reproduced by a second user 2026-06-01. No OpenAI response, no roadmap. |
| Claude (web + Android) | ✅ | ❌ | `anthropics/claude-ai-mcp` issue #146, filed 2026-04-04, **closed as "not planned."** Claude attempts the tool call, fails, then confabulates an answer. |
| Gemini app / Gemini Live | — | ⚠️ partial | Gemini Live *does* run tool calls mid-conversation, but only against Google's curated Connected Apps list (Calendar, Maps, Workspace, Spotify…). Custom MCP servers are API-side only. |

Gemini is the only one worth re-checking later — it's the sole platform already
executing tool calls inside a live voice session, so it is the most plausible to
open up. But a curated partner list is not something Gastrobrain can join.

**【推測】** The Claude issue being closed as *not planned* suggests this is
architectural: voice runs on a separate speech-to-speech path that doesn't
inherit the chat thread's tool registry. Not a near-term feature gap.

### Layer 0 — the ¥0 thing that does work today

Worth knowing as a stopgap: dictate into claude.ai or ChatGPT with the phone
keyboard mic, let the MCP connector answer in text, have the OS read it back.
Not a live conversation — no interruption, no back-and-forth — but it is
voice-in / voice-out over the existing connector, available now, for free.
Useful as a comparison point when demoing the real thing.

---

## Layer 2a — OpenAI Realtime API

**What it does.** A native speech-to-speech model (`gpt-realtime-2.1`). Audio
in, audio out, no transcription round-trip — so it handles interruption,
backchannel ("うん、はい"), and tone the way a person does. Connects over
WebRTC, WebSocket, or SIP (a real phone number).

**What we'd have to build.**

1. A `POST /voice/session` route in `web_api.py` that mints an ephemeral
   Realtime client secret. Because our backend already knows who the signed-in
   user is, it injects **that user's PAT** into the MCP header at session
   creation:

   ```json
   { "type": "mcp",
     "server_label": "gastrobrain",
     "server_url": "https://<cloud-run-url>/mcp/",
     "headers": { "Authorization": "Bearer tok_xxx" },
     "allowed_tools": ["search_knowledge"],
     "require_approval": "never" }
   ```

2. A WebRTC page in the web app (mic permission, connect, transcript display).
3. PAT auto-minting so users don't have to visit 設定 → MCP連携 first.

**Quality.** Best-in-class conversational feel; Japanese is strong. The
limitation is the voice itself — OpenAI ships a fixed set of preset voices and
**explicitly does not allow custom or cloned voices**, as an anti-impersonation
measure. They've said they may explore it later. So the agent will sound like a
capable stranger, not like anyone at Gastroduce.

**Cost.** `gpt-realtime-2.1` is $32/1M audio-input tokens and $64/1M
audio-output. Roughly 600 tokens per minute listening, 1,200 speaking. With
prompt caching a typical agent lands at **$0.06–0.11/min**; the mini variant at
**$0.02–0.05/min**.

At 10 users × 10 min/day × 20 days = 2,000 min/月:
- full model: **¥18,000–33,000/月**
- mini: **¥6,000–12,000/月**

【推測】 assumes ¥150/$; actual mix of listening vs. speaking moves this ±40%.
For comparison, 10 ChatGPT Plus seats is ~¥30,000/月 — at our usage level,
metered voice is *cheaper* than a per-seat product, not an addition to it.

**Effort: 2–4 days.** No new infrastructure; it's one route plus a page.

**This is the only option where per-user NotePM access control survives
unchanged**, because the session is minted server-side after authentication.

---

## Layer 2b — ElevenLabs Agents

**What it does.** A managed conversational agent platform with native remote
MCP support (both SSE and streamable HTTP) — and, unlike OpenAI, **voice
cloning**.

**What we'd have to build.**

1. Record the boss's voice for a Professional Voice Clone: 30 minutes minimum,
   3 hours of consistent studio-grade audio recommended for production quality.
   Requires Creator tier ($22/月) or above.
2. Configure the agent in their dashboard: system prompt, MCP server URL, auth
   header, `allowed_tools`, approval mode.
3. Embed their widget or SDK in the web app.

Very little of this is code. Most of the effort is the recording session.

**Quality.** Best TTS on the market, and the cloning is genuinely convincing.
Architecture is cascaded (STT → LLM → TTS) rather than native speech-to-speech,
so turn-taking is marginally less fluid than 2a — but they've optimized hard for
this and it's close. They also ship a `pre_tool_speech` field that fills the
dead air while a tool runs, which directly helps our 600–1500 ms retrieval
latency.

**Cost.** Plans: Creator $22 / Pro $99 / Scale $299 / Business $990 per month,
with bundled agent minutes (Business includes ~12,375–13,750 min). Metered rate
is ~$0.08/min on an annual Business plan, $0.096/min overage. At 2,000 min/月 we
sit in Pro/Scale territory: **roughly ¥15,000–45,000/月**. Note the LLM cost
behind the agent may be billed separately — confirm before committing.

**Effort: 2–3 days of config, plus the recording session** — *if* the ACL
question below resolves favorably.

### ⚠️ The open blocker

ElevenLabs documents MCP credentials as **workspace-level static secrets**.
There is no documented way to vary the `Authorization` header per conversation.
If that's accurate, every voice user shares one Gastrobrain identity, and the
per-user NotePM ACL collapses to whatever that single token can see — which is
unacceptable for a corpus containing private Slack channels.

**This must be confirmed with ElevenLabs support before Layer 2b is viable.**
Two possible outs, both unverified: (a) dynamic variables usable inside MCP
headers, (b) one ElevenLabs agent per user, provisioned via their API. Option
(b) would work but adds real provisioning complexity.

Also note: MCP support is unavailable to workspaces on Zero Retention Mode.

---

## Layer 3 — build it ourselves (LiveKit / Pipecat + Fish Audio)

**What it does.** We assemble the pipeline: STT → our own LLM turn → TTS from a
cloning provider like Fish Audio, wired through LiveKit or Pipecat for
transport, VAD, and interruption handling.

**What we'd have to build.** Everything: turn detection, barge-in, endpointing,
audio transport, session state, deployment, and latency tuning — plus ongoing
maintenance as each component's API moves.

**Quality.** Entirely dependent on our tuning. Fish Audio itself is strong:
clones from a 15-second sample (1–3 min recommended), native-level Japanese,
sub-500 ms API latency with streaming. But a cascaded pipeline we tune ourselves
will realistically feel less fluid than either managed option for the first
several iterations.

**Cost.** Cheapest per unit, most expensive in aggregate. Fish Audio is
$15/1M UTF-8 bytes — but Japanese is 3 bytes/char, so effectively **$45/1M
Japanese characters**. Add STT, the LLM turn, and hosting. Component spend at
our volume is maybe **¥5,000–10,000/月**; the real cost is engineering time.

**Effort: 3–6 weeks, plus permanent maintenance.**

**When this is actually justified:** only if we need the cloned voice **and**
per-user ACL **and** Layer 2b's ACL blocker turns out to be unresolvable. Not
for voice cloning alone.

---

## Does voice cloning force Layer 3?

**No.** This was the question that prompted this document, and the answer is
that cloning moves us from Layer 2a to Layer 2b — a vendor switch, not a
rebuild. ElevenLabs is a managed platform that does cloning *and* MCP.

The real trade is a three-way one, and no single option wins outright:

| Want | Option | Give up |
|---|---|---|
| Per-user ACL + cheapest + best turn-taking | 2a OpenAI | the boss's voice |
| The boss's voice, minimal build | 2b ElevenLabs | per-user ACL (unless resolved) |
| Both | 3 self-built | 3–6 weeks + ongoing maintenance |

### One non-technical note

A Professional Voice Clone requires the speaker's verified consent — ElevenLabs
enforces this with a spoken verification step, so the boss cloning his own voice
is straightforward. Worth deciding separately, though, how the agent discloses
itself: an internal tool answering NotePM questions in the boss's voice could be
mistaken for the boss actually having said something. A spoken disclaimer at
session start costs nothing and removes the ambiguity.

---

## Recommended next steps

1. Decide whether OpenAI receiving corpus snippets is acceptable — MCP tool
   calls execute server-side at the provider, so this adds a data processor
   alongside Anthropic and Cohere. Applies to Layer 2b equally.
2. Ask the boss whether the cloned voice is a genuine requirement or a
   nice-to-have. This single answer picks 2a vs. 2b.
3. If 2b is in play, email ElevenLabs support about per-conversation MCP
   auth headers before any recording happens.
4. Build the Layer 2a prototype regardless — it's 2–4 days, it's the fallback
   for every branch, and it makes the demo concrete.
5. Restrict `allowed_tools` to `search_knowledge` at first. `query_sales` has a
   60 s timeout; a minute of silence mid-conversation is unusable in voice.
6. Pre-warm the session: `mcp_list_tools` blocks the first turn while the tool
   list loads.
7. Re-check Gemini Live's connected-apps program in ~6 months.

---

## Sources

- [OpenAI — Realtime with tools (MCP)](https://developers.openai.com/api/docs/guides/realtime-mcp)
- [OpenAI — Introducing gpt-realtime](https://openai.com/index/introducing-gpt-realtime/)
- [InfoWorld — OpenAI adds MCP and SIP to gpt-realtime](https://www.infoworld.com/article/4048375/openai-adds-mcp-and-sip-support-to-gpt-realtime-for-smarter-voice-based-agents.html)
- [HackerNoon — Realtime API pricing from 4,000 measured sessions](https://hackernoon.com/openai-realtime-api-pricing-in-2026-real-world-data-from-4000-measured-sessions)
- [Microsoft Q&A — GPT Realtime voice selection (no custom voices)](https://learn.microsoft.com/en-us/answers/questions/5698273/gpt-realtime-api-how-to-specify-the-voice)
- [ElevenLabs — MCP documentation](https://elevenlabs.io/docs/eleven-agents/customization/tools/mcp)
- [ElevenLabs changelog 2026-04-27](https://elevenlabs.io/docs/changelog/2026/4/27)
- [Coval — ElevenLabs voice cloning review 2026](https://www.coval.ai/blog/elevenlabs-review-2026-voice-cloning-and-synthesis-capabilities-explained/)
- [CloudTalk — ElevenLabs pricing for AI calling 2026](https://www.cloudtalk.io/blog/elevenlabs-pricing/)
- [OpenAI community — MCP in voice mode on web/Android](https://community.openai.com/t/chatgpt-support-of-mcp-in-voice-mode-on-web-and-android/1382072)
- [anthropics/claude-ai-mcp issue #146](https://github.com/anthropics/claude-ai-mcp/issues/146)
- [9to5Google — Gemini Live connected apps (2026-05-23)](https://9to5google.com/2026/05/23/gemini-live-connected-apps/)
- [TextToLab — Fish Audio pricing 2026](https://texttolab.com/blog/fish-audio-pricing)
- [Fish Audio — TTS API with voice cloning](https://fish.audio/blog/best-text-to-speech-api-voice-cloning/)
