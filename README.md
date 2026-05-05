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

This repo uses [`uv`](https://docs.astral.sh/uv/) for dependency
management. It depends on its sibling `psyneulink-agent` as an
editable install (configured via `[tool.uv.sources]` in
`pyproject.toml`), so the parent folder layout matters:

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

## Runtime dependencies

The UI process itself is small; the heavy work happens downstream
through agent core and the MCP. Two external pieces are required at
runtime (but not for tests):

- **`ANTHROPIC_API_KEY`** in the environment. The chat pane drives
  Anthropic's Messages API through agent core; without a key,
  `send_user_message` will raise on the first turn.
- **`graphviz` system binary**. The MCP's
  `render_composition_graph` tool shells out to `dot` to render
  PNGs. On macOS: `brew install graphviz`. On Debian/Ubuntu:
  `apt install graphviz`. The UI starts fine without it — you'll
  just see graph-render errors when you try to view a composition.

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
