# Gastrobrain

Internal Q&A over Gastroduce's NotePM knowledge base.

## Docs

- [`docs/PRD.md`](docs/PRD.md) — design, scope, eval plan
- [`docs/SETUP.md`](docs/SETUP.md) — one-time GCP / Supabase / Slack setup
- [`docs/SLACK_DEPLOY.md`](docs/SLACK_DEPLOY.md) — Cloud Run deploy checklist
- [`docs/MCP.md`](docs/MCP.md) — connect any agent (Claude Code, Cursor, …) in one command
- [`docs/archive/memo.md`](docs/archive/memo.md) — original proposal (superseded by PRD §1)

## Quick start

```bash
# Install (one-time; requires uv: https://docs.astral.sh/uv/)
uv sync

# Drop markdown files into ./corpus/ — see corpus/README.md for format
uv run gb-ingest corpus/

# Ask a question
uv run gb-ask "EC在庫の発注ロジックは？"
```

## Connect agents (MCP)

Any MCP-aware agent (Claude Code, Cursor, Claude Desktop, claude.ai) can
search Gastrobrain in one command:

```bash
claude mcp add --transport http gastrobrain \
  https://<cloud-run-url>/mcp \
  --header "Authorization: Bearer tok_xxx"
```

See [`docs/MCP.md`](docs/MCP.md) for tokens, other clients, and tool
schemas.

## Stack

- Postgres (Supabase, Tokyo) with `pgvector` (HNSW) + `pgroonga` (JP FTS via MeCab)
- Embeddings: Cohere `embed-multilingual-v3.0` (1024-dim)
- Reranker: Cohere `rerank-multilingual-v3.0`
- LLM: Anthropic `claude-sonnet-4-6` with prompt caching
- Tracing: Langfuse (jp.cloud.langfuse.com)
