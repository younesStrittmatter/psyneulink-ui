# psyneulink-ui

The web frontend for the `psyneulink-ai` stack. **Top of the stack.**
A FastAPI process serving a two-pane vanilla-JS app (chat + live
composition graph) on `localhost`. Glue, not logic.

## Working with Claude on this project

I'm using this project to learn how to use Claude (Code, API, SDK, MCP)
efficiently and in a modern way. While we work:

- Surface better tools and idioms.
- Suggest, don't silently do.
- Flag anti-patterns.
- Be concrete.

(Mirrors the rule in `psyneulink-mcp/CLAUDE.md`.)

## Architecture (five sibling repos)

This repo is one of five siblings under `psyneulink-ai/`:

- **`psyneulink-mcp`:** passive MCP server wrapping PsyNeuLink. Never
  imported here.
- **`psyneulink-corpus`:** community-curated brainlikes + tool feedback
  Issues. Never imported here.
- **`psyneulink-agent`:** Layer-2 modeling agent. The only thing this
  repo imports. We instantiate `psyneulink_agent.core.Session` and
  drive it through its public API (`lifespan`, `send_user_message`,
  `call_tool`, `attach`, `detach`, `snapshot`).
- **`psyneulink-psyche`:** behavioral data convention. Never imported
  here. Uploaded CSVs reach PSYCHE through MCP tools, never through
  this process.
- **`psyneulink-ui` (this repo):** FastAPI + vanilla JS web shell.

## Separation of concerns is pure (hard rule)

- **Imports `psyneulink-agent` only.** Never `psyneulink-mcp`, never
  `psyneulink-psyche`, never `psyneulink` itself. If you find yourself
  reaching for any of those, the right move is to expose the capability
  as a `Session` method (in agent core) or as an MCP tool that
  `Session.call_tool` can hit.
- **No domain logic.** Every UI affordance maps to one thin call into
  `Session`. Modeling decisions are the agent's job; the UI just
  renders state and forwards user gestures.
- **Localhost only, single user, no auth.** This is a developer tool,
  not a product. Multi-user / hosted is explicitly out of scope.
- **Agent core is the boundary.** Removing this repo entirely must not
  break `--chat-sdk` or `--run`. The other front-ends never look here.

## The two-pane mental model

```
┌──────────────────────────┬──────────────────────────┐
│ chat pane (left)         │ graph pane (right)       │
│                          │                          │
│ user types a message     │ <img src=data:...> of    │
│ → SSE stream of          │ composition.show_graph() │
│   text_chunk / tool_use  │                          │
│   / tool_result events   │ refreshed by polling     │
│   from Session.send_     │ get_composition_revision │
│   user_message           │ every 1.5s; only re-     │
│                          │ fetches the PNG when     │
│ resource dock (below):   │ the revision number      │
│ upload PDFs / data /     │ has bumped               │
│ .py model files          │                          │
└──────────────────────────┴──────────────────────────┘
```

The chat pane is push (SSE from the LLM loop). The graph pane is pull
(cheap polling). They share a `Session` and a `lifespan()` so MCP-side
state — handle registry, journal, revision counters — survives across
turns and across direct `call_tool` invocations.

## Vanilla frontend; if you reach for React, stop and read this first

The frontend is one HTML file, one CSS file, one JS file, no build
step, no `node_modules`. This is deliberate:

- The UI is intentionally small (two panes + a dock + a textbox). It
  doesn't need a virtual DOM.
- `node_modules` introduces a second package manager into a Python
  repo and dwarfs the rest of the stack.
- Every framework migration we don't pre-commit to is one less
  rewrite when the UI's role changes.

If a feature seems to need React/Vue/Svelte/Vite/etc., the burden of
proof is on the new framework. Most likely the feature should live in
agent core anyway (where every front-end gets it), not here.

Tooling we will *not* add without an explicit, written reason:
- React, Vue, Svelte, SolidJS, htmx
- Vite, Webpack, esbuild, Parcel, Rollup
- npm, yarn, pnpm, bun
- TypeScript, JSX, SCSS

WebSockets are also rejected for the MVP — SSE is one-way (server →
browser), which is what we want, and is dramatically simpler to test.

## Layout

```
src/psyneulink_ui/
  __init__.py
  server.py        # FastAPI app + lifespan + static mount
  state.py         # UISession registry; one Session per browser tab
  routes.py        # /api/* endpoints (chat SSE, graph, resources)
  sse.py           # tiny SSE encoders
  cli.py           # `psyneulink-ui` -> uvicorn
  static/
    index.html     # two-pane layout
    app.js         # SSE chat + graph poll + uploads (vanilla)
    style.css      # minimal modern dark theme
tests/
  conftest.py      # FakeSession + autouse registry reset
  test_state.py
  test_routes.py
  test_cli.py
```

## Cross-repo coupling notes

- **API contract with agent core**: this repo calls
  `Session.lifespan()`, `Session.send_user_message(text)`,
  `Session.call_tool(name, args)`, `Session.attach(resource)`,
  `Session.detach(resource)`, `Session.snapshot()`,
  `Session.system_prompt()`. If any of those signatures move, this
  repo breaks. They are part of the agent's stable surface.
- **MCP tool names**: the graph pane depends on the MCP tool names
  `render_composition_graph` and `get_composition_revision`. Save /
  load buttons depend on `export_python_script` and
  `load_python_script`. These names live in `psyneulink-mcp`. If they
  rename, the rename must land in lockstep here.
- **Handle prefixes**: we treat any tool-input string starting with
  `"h_"` as a composition handle for the "agent has been working on
  this composition recently" heuristic. If the MCP changes its
  composition handle prefix, update `routes.py`.
- **Known compromise**: the `/api/sessions/{sid}/tools` endpoint pokes
  `session._mcp` to list available tools, because there is no public
  `Session.list_tools()` yet. This is documented at the call site;
  promote to a real method on `Session` if it lasts.

## Multi-repo dev sessions: switch workspace first

A *multi-repo dev session* authors changes in more than one of the
five sibling repos in one sitting (e.g., adding an MCP tool that the
UI then surfaces, or coordinating a `Session` API rename that has to
land in two repos together).

If you find you need one, **stop and ask the user to open a new Cursor
chat at the parent folder**:

```
~/Documents/code/AutoGrad/psyneulink-ai/
```

That folder has its own `AGENTS.md` and is the correct workspace for
multi-repo dev sessions. The shell sandbox restricts writes to the
workspace root; running cross-repo writes from this sub-repo workspace
forces a permission prompt for every shell call into a sibling. Don't
work around it with `required_permissions: ["all"]` — switch workspaces
once, work freely thereafter.

This is *not* the same as the forbidden cross-repo coupling above. A
multi-repo dev session produces independent commits in independent
repos that each respect the boundary. **Smell test:** if the work
would survive being done in two separate chats on different days with
no shared state, it's a dev-session convenience. If it requires
runtime/import coupling between repos, the polyrepo rule applies and
the design is wrong — fix the design.

## Stack

- `uv` for deps and venvs.
- `pyproject.toml` only.
- `fastapi` + `uvicorn[standard]` for the HTTP/SSE server.
- `python-multipart` for file uploads.
- `psyneulink-agent` (sibling, editable install) for the modeling
  Session.
- `pytest` + `httpx` for tests; FastAPI's `TestClient` is sufficient.
- `ruff` for lint+format.

## Workflow

1. `uv sync` to set up the venv (resolves the editable sibling).
2. `uv run pytest -q` for tests (no MCP / Anthropic required; tests
   mock `Session`).
3. `uv run ruff check src tests` for lint.
4. `uv run psyneulink-ui` to start the server. Open
   `http://127.0.0.1:8000/` in a browser.

System dependencies for the *full* run (not for tests):

- `ANTHROPIC_API_KEY` in the environment (for the chat path through
  agent core).
- `graphviz` system binary (`brew install graphviz`) for the graph
  pane to render compositions.

Both are checked at runtime by agent core / the MCP, not by this
process. The UI itself starts fine without them — you'll just see
errors in tool results when those code paths are exercised.

## Cross-link

- Parent conventions: `../AGENTS.md`.
- Plan that birthed this repo:
  `../psyneulink-agent/plans/ui-pdfs-psyche.md` (part 2).
