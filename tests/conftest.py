"""Pytest fixtures for psyneulink-ui.

Why all the mocking? The UI repo's test surface is "does this HTTP
endpoint translate into the right ``Session`` call?". We don't want
tests to depend on a running MCP server or on an Anthropic API key —
both are external systems owned by the agent core / MCP repos. So we
inject a ``FakeSession`` into the registry and assert against its
mock methods.

Two fixtures matter:

* ``patch_session`` swaps ``state.Session`` (the symbol the registry
  imports) for ``FakeSession``. New UI sessions created during the
  test will own a ``FakeSession`` instance.
* ``client`` returns a ``fastapi.testclient.TestClient`` bound to a
  fresh app and a fresh registry. The autouse ``reset_registry``
  fixture wipes ``state.REGISTRY._sessions`` between tests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from psyneulink_ui import state
from psyneulink_ui.server import create_app


class FakeSession:
    """Stand-in for ``psyneulink_agent.core.Session`` used by every test.

    Mirrors the public surface our routes consume (``model``, ``history``,
    ``resources``, ``attach``, ``detach``, ``snapshot``, ``system_prompt``,
    ``send_user_message``, ``call_tool``, ``lifespan``). Methods that the
    routes call are ``MagicMock``/``AsyncMock`` so tests can assert on
    args. ``send_user_message`` is a real async generator because the
    chat endpoint actually iterates over its yielded events.
    """

    def __init__(self) -> None:
        self.model = "fake-model"
        self.history: list[dict[str, Any]] = []
        self.resources: list[Any] = []
        self.mcp_project = None

        # ``list_tools_for_ui`` reaches in for ``_mcp``; give it any
        # truthy value so the "in lifespan" check passes. Tests that
        # exercise the tools endpoint can override it further.
        self._mcp = MagicMock()

        self.attach = MagicMock(side_effect=lambda r: self.resources.append(r))
        self.detach = MagicMock(side_effect=lambda r: self.resources.remove(r))
        self.system_prompt = MagicMock(return_value="fake system prompt")

        # call_tool: tests override return_value as needed.
        self.call_tool = AsyncMock(
            return_value='{"composition": "h_demo", "revision": 1}'
        )

        # send_user_message must be an *async generator function*, not
        # an AsyncMock — we iterate over it in the route handler.
        async def _gen(text: str, *, anthropic_client: Any | None = None):
            self.history.append({"role": "user", "content": text})
            yield {"type": "text_chunk", "text": "ok"}
            yield {
                "type": "tool_use",
                "id": "tu_1",
                "name": "render_composition_graph",
                "input": {"composition": "h_demo"},
            }
            yield {
                "type": "tool_result",
                "id": "tu_1",
                "name": "render_composition_graph",
                "content": '{"composition":"h_demo","revision":2}',
                "is_error": False,
            }
            yield {"type": "turn_complete", "stop_reason": "end_turn"}

        self.send_user_message = _gen

    def snapshot(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "history": list(self.history),
            "resources": [
                {"kind": getattr(r, "kind", lambda: "?")(),
                 "label": getattr(r, "label", lambda: "?")()}
                for r in self.resources
            ],
        }

    @asynccontextmanager
    async def lifespan(self):  # noqa: D401 — match real Session signature
        yield self


@pytest.fixture(autouse=True)
def reset_registry():
    state.REGISTRY._sessions.clear()
    yield
    state.REGISTRY._sessions.clear()


@pytest.fixture
def patch_session(monkeypatch):
    """Patch ``state.Session`` so ``REGISTRY.create()`` yields a ``FakeSession``."""
    monkeypatch.setattr(state, "Session", FakeSession)
    return FakeSession


@pytest.fixture
def client(patch_session):
    app = create_app()
    with TestClient(app) as c:
        yield c
