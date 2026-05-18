# Gastrobrain Web

ChatGPT-style web surface for Gastrobrain. Next.js 15 (App Router) + assistant-ui + Supabase Auth (Sign in with Slack). Calls the existing FastAPI backend (Cloud Run, Tokyo) via a same-origin proxy under `/api/*`.

## Quick start (local)

```bash
cd web
npm install
cp .env.example .env.local   # fill in the values below
npm run dev                  # http://localhost:3000
```

### Environment

| Var | Where to get it |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project → Settings → API → Project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase project → Settings → API → anon public |
| `GASTROBRAIN_API_URL` | Cloud Run URL of the FastAPI service (the one currently serving Slack) |

The backend also needs `SUPABASE_JWT_SECRET` (Supabase → Settings → API → JWT Secret) and `SUPABASE_PROJECT_URL` in its env — the web API router uses these to verify JWTs.

## One-time setup

1. **Apply the migration** to Supabase: `migrations/002_web_chat.sql` (adds `conversations`, `messages`, RLS policies).
2. **Enable Slack OAuth** in Supabase:
   - Authentication → Providers → Slack (OIDC) → enable
   - Configure the Slack app with redirect URL `https://<vercel-domain>/auth/callback` (and `http://localhost:3000/auth/callback` for dev)
   - Scopes: `openid email profile`
3. **Domain enforcement** is already done client-side in `app/auth/callback/route.ts` — non-`@gastroduce-japan.co.jp` accounts are signed out immediately. For defense-in-depth, optionally add a Supabase Auth Hook (Edge Function) that rejects at signup.
4. **Add the new envs to the Cloud Run service** alongside the existing Slack secrets:
   - `SUPABASE_JWT_SECRET`
   - `SUPABASE_PROJECT_URL`
5. **Deploy to Vercel**:
   - Root Directory: `web`
   - Framework Preset: Next.js
   - Set the three envs above
   - Deploy

## Architecture

```
Browser ──► Next.js (Vercel) ──► /api/chat (SSE proxy) ──► Cloud Run /v1/chat
                │                                              │
                ▼                                              ▼
       Supabase Auth (cookie)                          Supabase Postgres
       Slack OIDC provider                             conversations + messages + queries
```

The Next.js layer never touches the database directly — it proxies through the FastAPI service, which holds the Supabase service-account DB credentials. Ownership is enforced by the validated JWT `sub` claim + explicit `WHERE user_id = $1` on every query. RLS policies are defense-in-depth for any direct DB access.

## Key files

| Path | Role |
|---|---|
| `src/components/runtime-provider.tsx` | assistant-ui `useExternalStoreRuntime` bound to our Supabase-backed messages; opens `/api/chat` SSE on each user turn |
| `src/components/chat-thread.tsx` | Thread + composer using `ThreadPrimitive` / `MessagePrimitive` / `ComposerPrimitive` |
| `src/components/citation-chip.tsx` | Hoverable `[N]` chip with NotePM click-through |
| `src/components/thread-sidebar.tsx` | ChatGPT-style sidebar — plain React component, polls `/api/threads` every 30s |
| `src/lib/sse.ts` | Manual SSE parser (browser EventSource doesn't support POST + headers) |
| `src/lib/api.ts` | `forward()` helper used by every `/api/*` route to add `Authorization: Bearer <Supabase JWT>` |
| `middleware.ts` | Auth gate — redirects unauthenticated traffic to `/login` |

## Streaming invariant

`runtime-provider.tsx` mutates the placeholder assistant message **in place by id** as tokens arrive. Replacing it with a new message object spawns spurious assistant-ui branches. After the stream finishes, the local UUID is swapped for the server-issued `message_id` so feedback POSTs target the persisted row.

## Verifying end-to-end

1. `npm run dev`, sign in with Slack, ask a question — tokens should stream within ~2s, citation chips appear, click-through goes to NotePM.
2. Reload the page — sidebar shows the thread, click it, history rehydrates with citations.
3. Click 👎 on an answer — `queries.feedback` should update in Supabase Studio.
4. Ask a follow-up using a pronoun ("その単価は?") — the SSE stream should emit a `query_rewritten` event before retrieval; check Network → EventStream.

## Out of scope (deferred)

- Sharing / exporting threads
- Mobile-first layout (responsive is best-effort)
- Voice input, image attachments
- Slack ↔ web cross-surface message bridging
