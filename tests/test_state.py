"""Tests for the in-memory UI session registry.

We drive the async registry via ``asyncio.run`` so we don't need
pytest-asyncio as a dep — the registry's API is small enough that
one ``asyncio.run`` per test is fine.
"""

from __future__ import annotations

import asyncio
import uuid

from psyneulink_ui import state


def _run(coro):
    return asyncio.run(coro)


def test_create_session_returns_uisession_with_uuid_sid(patch_session):
    ui = _run(state.REGISTRY.create())
    assert ui.sid in state.REGISTRY._sessions
    # Sid should look like a UUID4 — let uuid parse it for us.
    parsed = uuid.UUID(ui.sid)
    assert parsed.version == 4
    # FakeSession was used.
    assert isinstance(ui.session, patch_session)


def test_get_returns_none_for_unknown_sid(patch_session):
    assert state.REGISTRY.get("not-a-real-sid") is None


def test_get_returns_session_after_create(patch_session):
    ui = _run(state.REGISTRY.create())
    assert state.REGISTRY.get(ui.sid) is ui


def test_close_removes_session(patch_session):
    ui = _run(state.REGISTRY.create())
    closed = _run(state.REGISTRY.close(ui.sid))
    assert closed is True
    assert state.REGISTRY.get(ui.sid) is None
    # Closing twice is a no-op (returns False, doesn't raise).
    closed_again = _run(state.REGISTRY.close(ui.sid))
    assert closed_again is False


def test_close_all_clears_all_sessions(patch_session):
    a = _run(state.REGISTRY.create())
    b = _run(state.REGISTRY.create())
    c = _run(state.REGISTRY.create())
    assert {a.sid, b.sid, c.sid} <= set(state.REGISTRY._sessions)
    _run(state.REGISTRY.close_all())
    assert state.REGISTRY._sessions == {}


def test_upload_dir_is_lazy_and_persistent(patch_session, tmp_path):
    ui = _run(state.REGISTRY.create())
    # Not created yet.
    assert ui._upload_dir is None
    d1 = ui.upload_dir()
    d2 = ui.upload_dir()
    assert d1 == d2
    assert d1.exists()
    # Cleanup happens on close.
    _run(state.REGISTRY.close(ui.sid))
    assert not d1.exists()
