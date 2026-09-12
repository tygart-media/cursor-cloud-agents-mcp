# cursor-cloud-agents-mcp

[![CI](https://github.com/tygart-media/cursor-cloud-agents-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/tygart-media/cursor-cloud-agents-mcp/actions/workflows/ci.yml) ![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

**An MCP server that lets your AI assistant drive Cursor Cloud Agents.** Launch agents on your repos (or repo-less for research), pick any Cursor model, follow up mid-run, fetch results, and check spend — from Claude Code, Cursor Desktop, Muse, or any MCP client.

If you've searched for *"Cursor Cloud Agents MCP server"*, *"MCP server for Cursor"*, *"connect Claude Code to Cursor"*, *"Cursor API MCP integration"*, or *"launch Cursor agents programmatically"* — this is it.

## Why this exists

Cursor's Cloud Agents API is powerful but raw: you manage agent/run lifecycles, idempotency, timeouts, and model IDs by hand. Every community MCP bridge we found was built on the retired v0 API and left unmaintained. This one is built on the **current v1 API** (`agent` + `run` split), bakes in the hard-won lessons (idempotent launches, generous timeouts, never trust a timeout as failure), and is tested against the live API.

It was designed in the open: the v1 spec was reviewed by four AI models, verified against Cursor's live docs and community reports, and the code was reviewed by two more before release.

## Quickstart

**1. Get a Cursor API key.** Cursor Dashboard → API Keys → mint a User API key. (Some guides point to Dashboard → Integrations; either location works — you want the `crsr_…` key.)

**2. Install.**

```bash
# Install from source (PyPI release coming):
uvx --from git+https://github.com/tygart-media/cursor-cloud-agents-mcp cursor-cloud-agents-mcp
# or: pip install git+https://github.com/tygart-media/cursor-cloud-agents-mcp
```

**3. Add it to your MCP client.** ⚠️ Set the key in the client's `env` block, not just your shell — MCP servers are spawned with a scrubbed environment and won't inherit shell variables.

Claude Code (`~/.claude.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "cursor-cloud-agents": {
      "command": "cursor-cloud-agents-mcp",
      "env": { "CURSOR_API_KEY": "crsr_…" }
    }
  }
}
```

Cursor Desktop (MCP settings):

```json
{
  "mcpServers": {
    "cursor-cloud-agents": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tygart-media/cursor-cloud-agents-mcp", "cursor-cloud-agents-mcp"],
      "env": { "CURSOR_API_KEY": "crsr_…" }
    }
  }
}
```

**4. Smoke test.** Ask your assistant: *"Use cursor_whoami to verify my Cursor key, then list my models."*

## Tools

| Tool | What it does |
|---|---|
| `cursor_launch` | Launch an agent + first run. Returns `agent_id` and `run_id`. Supports repos, repo-less, model pick, plan/agent mode, idempotency keys. |
| `cursor_status` | Poll a run's status. Run status is the source of truth (agent `ACTIVE` ≠ still running). |
| `cursor_result` | Wait for a run to finish and return its result (truncated safely). |
| `cursor_followup` | Send a follow-up; starts a new run (returns the new `run_id`). |
| `cursor_cancel` | Cancel the active run (terminal; follow up to continue). |
| `cursor_list` | List agents, newest first — your reconciliation tool. |
| `cursor_models` | List model ids + params + variants. Call before launching; ids aren't guessable. |
| `cursor_whoami` | Verify auth / identity. |
| `cursor_usage` | Token usage and cost per agent or run. |

### Launching well

Give the agent a complete prompt: goal, constraints, and how to verify it's done. Vague prompts produce vague agents. Omit `repo_url` for research/review/writing tasks; pass a GitHub https URL when it should write code. Omit `starting_ref` to use the repo default — never assume `main`. Omit `model` entirely to use your default chain (never send the literal string `"Auto"` — the API rejects it).

## Transports

- **`rest`** (default): direct `https://api.cursor.com` with `CURSOR_API_KEY`. Works anywhere.
- **`sandbox`**: shells out to a `cursor-agent` CLI on PATH, for sandboxed agent environments that broker the credential for you. Select with `CURSOR_TRANSPORT=sandbox`. Auto-detected when a CLI exists but no API key is set.

## Troubleshooting

- **Launch reported `status: "unknown"`** — the API accepted the launch but the response was slow (launches can take minutes). The agent almost certainly exists: run `cursor_list` and match by creation time, or retry the launch with the same `idempotency_key` — replays can never duplicate.
- **`cursor_followup` says "agent is busy"** — a run is still active. Poll `cursor_status` until terminal, then follow up.
- **Auth errors** — run `cursor_whoami`. Keys are minted at Dashboard → API Keys.
- **"I launched an agent but can't see it in Cursor"** — API-created agents are hidden from the dashboard's default list; use the Source filter.
- **Key in logs** — the client scrubs `CURSOR_API_KEY` from every error path, but still: never paste your key into a prompt.

## What it costs

Cloud agents spend real Cursor quota. Use `cursor_usage` to check per-run cost. This server makes no billing decisions for you.

## License

MIT.
