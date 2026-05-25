# MCP — connect any agent to Gastrobrain

Gastrobrain exposes its retrieval pipeline as an **MCP server** so that any
Model Context Protocol-aware agent — Claude Code, Claude Desktop, Cursor,
claude.ai connectors, anything else — can search the internal NotePM corpus
without copy-pasting bearer tokens.

The MCP server is **search-only**. It returns ranked chunks with citations;
the calling agent uses its own LLM to produce the final answer. No Sonnet
spending happens on the Gastrobrain side for MCP traffic.

- Endpoint: `https://<cloud-run-url>/mcp/` (trailing slash required)
- Transport: Streamable HTTP (JSON responses, stateless)
- Auth: **OAuth 2.1** (recommended) — browser-based Google sign-in
- Auth fallback: bearer Personal Access Token (for CI / scripts)
- Tool surface: `search_knowledge(query, top_k=8, min_score=0.20)`

> **Trailing slash:** Cloud Run 307-redirects `/mcp` → `/mcp/`, and most
> MCP clients don't follow the redirect. Always register the canonical URL
> with the trailing slash.

---

## 1. Recommended: OAuth sign-in (no tokens)

Every supported MCP client speaks OAuth 2.1 with PKCE and dynamic client
registration. The first time you `claude mcp add` Gastrobrain, the client:

1. Hits `/mcp/`, gets a 401 with `WWW-Authenticate: Bearer resource_metadata=...`
2. Discovers the AS via `/.well-known/oauth-protected-resource` →
   `/.well-known/oauth-authorization-server`
3. Dynamically registers itself at `/oauth/register`
4. Opens a browser to `/oauth/authorize`, which redirects to Google
5. You sign in with your `@gastroduce-japan.co.jp` Google account
6. Browser → `/oauth/google-callback` → our `/oauth/token` → access + refresh
   tokens are stored by the client
7. Subsequent calls just work. Access tokens auto-refresh.

If your account isn't in the `@gastroduce-japan.co.jp` workspace the sign-in
is rejected at the consent step.

### Claude Code

```bash
claude mcp add --transport http --scope user gastrobrain \
  https://<cloud-run-url>/mcp/
```

Browser opens, you sign in, done. `claude mcp list` should show `✓ Connected`.

### Cursor

Either use the GUI (Settings → MCP → Add MCP server) or edit
`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "gastrobrain": {
      "type": "streamable-http",
      "url": "https://<cloud-run-url>/mcp/"
    }
  }
}
```

Restart Cursor; the first invocation pops the browser for sign-in.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) with the same JSON shape, then restart the app.

### claude.ai connectors

Settings → Connectors → **Add custom connector**:

- URL: `https://<cloud-run-url>/mcp/`
- Click the **Authenticate** button after saving — Google sign-in opens

Tokens are stored in your claude.ai account; no local config.

### Managing sessions

`https://<web-app-url>` → 設定 → MCP連携 → **アクティブなセッション** lists every
client that's currently signed in (with last-used dates). The trash-can icon
revokes a session — that client will have to re-authenticate on its next call.

---

## 2. Personal Access Tokens (for CI / scripts)

PATs exist for places OAuth isn't practical: CI jobs, scripted automations,
servers without a browser. They're long-lived bearer tokens you copy-paste
into a config.

### Mint a PAT

Open the web app → 設定 → MCP連携 → expand **Personal Access Token (CI / スクリプト用)**
→ **新しい PAT を発行**. The raw token is shown once. Treat it like a password.

### Use a PAT

```bash
claude mcp add --transport http --scope user gastrobrain \
  https://<cloud-run-url>/mcp/ \
  --header "Authorization: Bearer tok_xxxxxxxxxxxx"
```

```json
{
  "mcpServers": {
    "gastrobrain": {
      "type": "streamable-http",
      "url": "https://<cloud-run-url>/mcp/",
      "headers": {
        "Authorization": "Bearer tok_xxxxxxxxxxxx"
      }
    }
  }
}
```

PATs can be revoked from the same settings panel.

### Admin / break-glass tokens

The `GASTROBRAIN_MCP_TOKENS` secret in GCP Secret Manager holds
`label:tok_xxx,label2:tok_yyy` pairs that work even when DB-backed tokens
don't (e.g., during migrations). Admins mint these manually:

```bash
python -c "import secrets; print('tok_' + secrets.token_urlsafe(32))"
echo -n "<existing>,opslabel:tok_xxx" \
  | gcloud secrets versions add GASTROBRAIN_MCP_TOKENS --data-file=-
gcloud run services update gastrobrain --region asia-northeast1
```

---

## 3. Using `search_knowledge`

Inside any of those agents, just ask a normal question:

> Search Gastrobrain for 楽天の在庫発注ルール and summarise.

The agent calls `search_knowledge`, gets the top reranked chunks, and writes
its own answer with `[N]` citations referencing the `doc_title` / `doc_url`
we returned.

`search_knowledge` arguments:

| Arg | Type | Default | Notes |
|---|---|---|---|
| `query` | str | (required) | Japanese is primary; English works |
| `top_k` | int | 8 | clamped to [1, 20] |
| `min_score` | float | 0.20 | rerank floor; matches the Slack/web default |

Result fields: `chunk_id`, `doc_id`, `doc_title`, `doc_url`, `heading_path`,
`snippet`, `rerank_score`.

---

## 4. Operating notes

- **Telemetry**: every MCP call writes a row to `queries` with
  `user_id = 'mcp:<label>'`. For OAuth tokens, `<label>` is the email
  prefix (e.g., `mcp:itsuki.son`). For PATs, it's the label you minted under.
- **Cost per call**: ~1 Cohere embed + 1 Cohere rerank ≈ ¥0.3 (no Sonnet
  spend). Roughly 10× cheaper than a Slack reply.
- **Latency**: typically 600–1500 ms (Cohere dominates). No streaming —
  the response is a single JSON list.
- **Rate limits**: none in v1. If we see abuse, add per-token bucketing.
- **OAuth lifetimes**: access tokens 1h, refresh tokens 30d with rotation.
  Revoke any session from the settings page to force re-auth within ~1h.
- **PAT lifetimes**: indefinite until revoked.
- **Disabling**: set `GASTROBRAIN_MCP_ENABLED=false` and redeploy. The
  `/mcp/` route stops being mounted on the next boot.

---

## 5. Why search-only

The MCP design principle is "servers provide context, clients run the
model." Returning generated answers would re-run Sonnet on Gastrobrain's
side after the caller's LLM already has the conversation context — that's
double LLM spend plus lost personalization.

Returning chunks + citations lets every caller decide how to render the
answer, in their own conversational voice. The Slack and web surfaces still
do `ask_gastrobrain(question)` (with a finished answer); for the chat
experience, use those.

---

## 6. Local dev / debugging

Spin up the server locally (OAuth disabled — use a static token):

```bash
export GASTROBRAIN_MCP_TOKENS="dev:tok_localtest"
uv run uvicorn gastrobrain.slack_app:app --reload --port 8080
```

List tools:

```bash
curl -s -H "Authorization: Bearer tok_localtest" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     http://localhost:8080/mcp/
```

For an interactive UI, use the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector \
  --transport http \
  --url http://localhost:8080/mcp/ \
  --header "Authorization=Bearer tok_localtest"
```

OAuth flows can't easily run against `localhost` because Google won't
redirect to non-https origins (except loopback for our own callback, which
isn't the right configuration here). Use static tokens for local dev; test
OAuth against the deployed Cloud Run service.
