"""In-memory UI session registry.

One UI session corresponds to one browser tab. Each owns:

* a ``psyneulink_agent.core.Session`` instance,
* an active ``Session.lifespan()`` context (held open via an
  ``AsyncExitStack``) so the MCP connection — and therefore the handle
  registry, journal, and composition revision counters — survives
  across HTTP requests,
* a session-scoped temp dir for resource uploads,
* a small bag of UI-only metadata (the last composition handle the
  agent worked with, the last revision number we've observed for it).

The registry is a process-singleton: ``REGISTRY``. Tests use the
``patch_session`` fixture to swap in a ``FakeSession`` so they don't
touch a real MCP / Anthropic.

Lifespan ownership belongs here, not in ``server.py``: per-UI-session
``lifespan`` is per-browser-tab, not per-process. ``server.py`` only
runs ``REGISTRY.close_all()`` once at app shutdown.
"""

from __future__ import annotations

import tempfile
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psyneulink_agent.core import Session

# Heuristic prefix the MCP uses for composition handles. We watch
# tool-input strings starting with this prefix as a "agent has been
# working on this composition recently" signal so the graph pane can
# default to it. If the MCP ever changes the prefix, update here.
COMPOSITION_HANDLE_PREFIX = "h_"


@dataclass
class UISession:
    """One browser tab's worth of state."""

    sid: str
    session: Session
    stack: AsyncExitStack
    # Most recently-seen composition handle in any tool_use input.
    # Used by ``GET /api/sessions/{sid}/graph`` when the caller doesn't
    # pass a ``composition`` query param.
    active_composition: str | None = None
    # Last revision number we've observed for ``active_composition``.
    # The frontend can compare this against a fresh
    # ``get_composition_revision`` poll to decide whether to re-fetch
    # the PNG.
    last_revision: int = 0
    # Lazily-created session-scoped tempdir for resource uploads.
    # Held inside the AsyncExitStack so it gets cleaned up on close.
    _upload_dir: Path | None = field(default=None, repr=False)

    def upload_dir(self) -> Path:
        """Return (creating on first call) the tempdir for resource uploads."""
        if self._upload_dir is None:
            tmp = tempfile.TemporaryDirectory(prefix=f"psyneulink-ui-{self.sid[:8]}-")
            self.stack.callback(tmp.cleanup)
            self._upload_dir = Path(tmp.name)
        return self._upload_dir


class UISessionRegistry:
    """Process-wide map of ``sid -> UISession``."""

    def __init__(self) -> None:
        self._sessions: dict[str, UISession] = {}

    async def create(self) -> UISession:
        """Mint a new UI session.

        Spins up a fresh ``Session`` and immediately enters its
        ``lifespan()`` context manager so subsequent ``send_user_message``
        and ``call_tool`` invocations share one MCP connection.
        """
        sid = str(uuid.uuid4())
        sess = Session()
        stack = AsyncExitStack()
        await stack.enter_async_context(sess.lifespan())
        ui = UISession(sid=sid, session=sess, stack=stack)
        self._sessions[sid] = ui
        return ui

    def get(self, sid: str) -> UISession | None:
        return self._sessions.get(sid)

    async def close(self, sid: str) -> bool:
        """Tear down a single UI session. Returns True if it existed."""
        ui = self._sessions.pop(sid, None)
        if ui is None:
            return False
        await ui.stack.aclose()
        return True

    async def close_all(self) -> None:
        """Tear down every UI session. Called from FastAPI's shutdown hook."""
        for sid in list(self._sessions.keys()):
            await self.close(sid)


REGISTRY = UISessionRegistry()


async def list_tools_for_ui(session: Session) -> list[dict[str, Any]]:
    """Return ``[{"name", "description"}, ...]`` for the active MCP session.

    Implementation note (intentional compromise): there's no public
    ``Session.list_tools()`` yet. Until there is, we reach in through
    ``session._mcp`` (only valid inside ``lifespan()``). The day this
    pattern earns a second caller it should be promoted to a real
    method on ``Session`` — see CLAUDE.md "Cross-repo coupling notes".
    """
    from psyneulink_agent.core.mcp_bridge import list_anthropic_tools

    if session._mcp is None:
        raise RuntimeError(
            "UI session is not inside lifespan() — list_tools_for_ui can't reach MCP"
        )
    tools = await list_anthropic_tools(session._mcp)
    return [{"name": t["name"], "description": t.get("description", "")} for t in tools]
