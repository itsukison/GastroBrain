# MCP — connect any agent to Gastrobrain

Gastrobrain exposes its retrieval pipeline as an **MCP server** so that any
Model Context Protocol-aware agent — Claude Code, Claude Desktop, Cursor,
claude.ai connectors, anything else — can search the internal NotePM corpus
in one command.

The MCP server is **search-only**. It returns ranked chunks with citations;
the calling agent uses its own LLM to produce the final answer. No Sonnet
spending happens on the Gastrobrain side for MCP traffic.

- Endpoint: `https://<cloud-run-url>/mcp`
- Transport: Streamable HTTP (JSON responses, stateless)
- Auth: `Authorization: Bearer <token>`
- Tool surface: `search_knowledge(query, top_k=8, min_score=0.20)`

---

## 1. Get a token

Tokens are minted manually and stored in GCP Secret Manager as
`GASTROBRAIN_MCP_TOKENS` in the form `label:secret,label2:secret2,…`.

Ask the admin (currently @itsuki) for a token, or mint one yourself:

```bash
python -c "import secrets; print('tok_' + secrets.token_urlsafe(32))"
```

Then append it to the secret value:

```bash
echo -n "<existing-value>,yourname:tok_xxxxxxxx" \
  | gcloud secrets versions add GASTROBRAIN_MCP_TOKENS --data-file=-
# Trigger Cloud Run to pick up the new version:
gcloud run services update gastrobrain --region asia-northeast1
```

The label is what shows up as `user_id="mcp:<label>"` in the `queries` table,
so use something identifying (`itsuki`, `team-alpha`, `cursor-laptop`).

---

## 2. Attach in one command

### Claude Code

```bash
claude mcp add --transport http gastrobrain \
  https://<cloud-run-url>/mcp \
  --header "Authorization: Bearer tok_xxx"
```

Scope it with `--scope user` to make it available across all your projects.
Verify with `claude mcp list`; the server should appear and `search_knowledge`
should be discoverable.

### Cursor

Either use the GUI (Settings → MCP → Add MCP server) or edit
`~/.cursor/mcp.json` directly:

```json
{
  "mcpServers": {
    "gastrobrain": {
      "type": "streamable-http",
      "url": "https://<cloud-run-url>/mcp",
      "headers": {
        "Authorization": "Bearer tok_xxx"
      }
    }
  }
}
```

Restart Cursor.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) with the same JSON shape, then restart the app.

### claude.ai connectors

Settings → Connectors → **Add custom connector**:

- URL: `https://<cloud-run-url>/mcp`
- Auth: Bearer + paste your token

---

## 3. Using it

Inside any of those agents, just ask a normal question:

> Search Gastrobrain for 楽天の在庫発注ルール and summarise.

The agent will call `search_knowledge`, get the top reranked chunks, and
write its own answer with `[N]` citations referencing the `doc_title` /
`doc_url` we returned.

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
  `user_id = 'mcp:<label>'`. The existing Slack/web dashboards show MCP
  traffic alongside the other surfaces.
- **Cost per call**: ~1 Cohere embed + 1 Cohere rerank ≈ ¥0.3 (no Sonnet
  spend, since we don't generate). Roughly 10× cheaper than a Slack reply.
- **Latency**: typically 600–1500 ms (Cohere dominates). No streaming —
  the response is a single JSON list.
- **Rate limits**: none in v1. If we see abuse, add per-token bucketing.
- **Rotation**: drop the offending pair from `GASTROBRAIN_MCP_TOKENS` and
  redeploy. Tokens are stateless — no DB cleanup needed.
- **Disabling**: set `GASTROBRAIN_MCP_TOKENS=""` (or
  `GASTROBRAIN_MCP_ENABLED=false`) and redeploy. The `/mcp` route stops
  being mounted on the next boot.

---

## 5. Why search-only

The MCP design principle is "servers provide context, clients run the
model." Returning generated answers would re-run Sonnet on Gastrobrain's
side after the caller's LLM (Claude inside Claude Code, etc.) already has
the conversation context — that's double LLM spend plus lost personalization.

Returning chunks + citations lets every caller decide how to render the
answer, in their own conversational voice, with their own preferences. This
also keeps the MCP surface tiny and the blast radius minimal.

If you really want an `ask_gastrobrain(question)` tool that returns a
finished answer, the Slack and web surfaces already do that. Either DM
the bot or open chat.gastrobrain in the browser.

---

## 6. Local dev / debugging

Spin up the server locally:

```bash
export GASTROBRAIN_MCP_TOKENS="dev:tok_localtest"
uv run uvicorn gastrobrain.slack_app:app --reload --port 8080
```

List tools:

```bash
curl -s -H "Authorization: Bearer tok_localtest" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     http://localhost:8080/mcp/
```

Call the tool:

```bash
curl -s -H "Authorization: Bearer tok_localtest" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
          "params":{"name":"search_knowledge",
                    "arguments":{"query":"TTSのデイリーチェックリスト"}}}' \
     http://localhost:8080/mcp/
```

For an interactive UI, use the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector \
  --transport http \
  --url http://localhost:8080/mcp/ \
  --header "Authorization=Bearer tok_localtest"
```
