# psyneulink-ui

**Web frontend for the `psyneulink-ai` stack.**

A FastAPI process that serves a two-pane vanilla-JS app on
`localhost:8000`:

- **Chat pane (left)** — streams the modeling agent's responses
  (text + tool calls) over Server-Sent Events as it works.
- **Graph pane (right)** — live PNG of the most recently mutated
  PsyNeuLink composition, refreshed automatically when the
  composition's revision counter bumps.
- **Resource dock** — upload PDFs (papers), CSV / Parquet behavioral
  data, and `.py` model files. "Save current model as .py" button.

This repo is glue, not logic. Every UI affordance maps to one thin
call into [`psyneulink-agent`](../psyneulink-agent). Localhost only,
single user, no auth.

## Install

### 1. System dependency: graphviz

The graph pane shells out to the `dot` binary to render PNGs. Install
it before anything else — the UI starts without it but the right pane
will show errors instead of graphs.

```bash
# macOS
brew install graphviz

# Debian / Ubuntu
sudo apt install graphviz
```

### 2. Python deps

This repo uses [`uv`](https://docs.astral.sh/uv/) and depends on its
sibling `psyneulink-agent` as an editable install (configured via
`[tool.uv.sources]`), so the parent folder layout matters:

```
psyneulink-ai/
├── psyneulink-agent/   # ← editable dep
└── psyneulink-ui/      # ← this repo
```

Then:

```bash
cd psyneulink-ui
uv sync
uv run pytest -q
```

### 3. LLM access (one of two backends)

The chat pane needs an LLM. You can use either of:

| Backend | What it uses | Auth | Set via |
|--------|-------------|------|----------|
| `sdk` (default) | Anthropic Messages API | `ANTHROPIC_API_KEY` env var | `export ANTHROPIC_API_KEY=sk-…` |
| `cli` | `claude` CLI (Claude Max plan or whatever your CLI is logged into) | `claude auth login` | install Claude Code, run `claude auth login` once |

The UI auto-detects: if `$ANTHROPIC_API_KEY` is set it picks `sdk`,
otherwise if `claude` is on `$PATH` it picks `cli`. Override with
`PSYNEULINK_LLM_BACKEND=sdk` or `PSYNEULINK_LLM_BACKEND=cli`.

Under the hood the CLI backend runs the MCP server over HTTP/SSE on a
free localhost port for the duration of the browser session, so both
the chat (via `claude --print --output-format stream-json --mcp-config
…`) and the graph pane (via direct `call_tool`) hit the same long-lived
MCP — handles persist across turns either way.

`graphviz` is required regardless of which backend you pick — the
graph pane renders compositions through the MCP server in both modes.

#### How to verify which backend was picked

The status line in the top-right of the UI shows `backend: sdk` or
`backend: cli`. Or, from a shell:

```bash
curl -s http://127.0.0.1:8000/api/sessions -X POST | jq .backend_kind
# → "sdk" or "cli"
```

#### Troubleshooting (CLI backend, Claude Max plan)

If the CLI path doesn't work on the first try:

1. **Auth once, in your shell.** `claude auth login`. The Anthropic
   plan that auths the CLI is the plan the UI's chat will burn
   tokens against; subscription accounts (Claude Max, Pro) work too.
2. **Use the same shell to launch the UI.** Auth state lives in your
   user keychain / config dir; if you `claude auth login` in iTerm
   and launch `uv run psyneulink-ui` from a fresh tmux pane that
   inherited a stripped env, the CLI may not see the credentials.
   `claude --version` should print a version *in the same shell* you
   then run `uv run psyneulink-ui` from.
3. **Force the backend if auto-detect picks wrong.** Auto-detect
   prefers `sdk` whenever `$ANTHROPIC_API_KEY` is set, even if
   you'd rather burn your Max plan than your API credit. Pin it
   explicitly:
   ```bash
   PSYNEULINK_LLM_BACKEND=cli uv run psyneulink-ui
   ```
4. **First turn hangs forever.** Usually a stale `claude` subprocess
   from a previous run still holds the SSE-MCP port. `pkill -f claude`
   and restart the UI.

## Run

```bash
uv run psyneulink-ui
```

This starts uvicorn on `127.0.0.1:8000`. Open it in a browser. A new
UI session is created automatically; the server holds one
`psyneulink_agent.Session.lifespan()` open per browser session, which
keeps the MCP connection (and therefore the handle registry, journal,
and composition revision counters) alive for the duration.

CLI flags:

```bash
uv run psyneulink-ui --host 0.0.0.0 --port 9999 --reload --log-level debug
```

## Out of scope (intentional)

- Multi-user / hosted deployment. **Localhost only.**
- Auth.
- Editing the model from the graph pane (view-only).
- Animated trial-by-trial dynamics. PNG re-render on revision bump
  is enough for MVP.
- React / Vue / Svelte / Vite / Webpack / npm. **Vanilla JS only.**
  No build step.

## Cross-link

- Sibling repos: `psyneulink-mcp`, `psyneulink-corpus`,
  `psyneulink-agent`, `psyneulink-psyche`.
- Parent conventions: `../AGENTS.md`.
- Conventions for this repo: `CLAUDE.md`.
- Plan that birthed this repo:
  `../psyneulink-agent/plans/ui-pdfs-psyche.md` (part 2).
