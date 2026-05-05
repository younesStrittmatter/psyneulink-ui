"""Tests for the FastAPI route layer.

These tests assert the *translation* layer: each HTTP endpoint maps
to the right ``Session`` call with the right args. They don't run a
real MCP, real Anthropic, or real graphviz — the ``patch_session``
fixture (see ``conftest.py``) replaces ``state.Session`` with a
``FakeSession`` so every assertion is local.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from psyneulink_ui import state

# ---------------------------------------------------------------------------
# session lifecycle
# ---------------------------------------------------------------------------


def test_post_session_returns_sid_and_model(client):
    r = client.post("/api/sessions")
    assert r.status_code == 200
    body = r.json()
    assert "sid" in body
    assert body["model"] == "fake-model"
    assert body["system_prompt_preview"] == "fake system prompt"


def test_post_session_includes_backend_kind(client):
    r = client.post("/api/sessions")
    assert r.status_code == 200
    body = r.json()
    # FakeSession defaults to backend_kind="sdk" — the wire field
    # should mirror that so the frontend can paint the status line on
    # the very first response without an extra round-trip.
    assert body["backend_kind"] == "sdk"


def test_get_session_includes_backend_kind(client):
    sid = client.post("/api/sessions").json()["sid"]
    snap = client.get(f"/api/sessions/{sid}").json()
    assert snap["backend_kind"] == "sdk"


def test_backend_kind_reflects_session_backend(client_factory):
    # When the underlying Session was minted with the CLI backend,
    # both endpoints should report ``"cli"`` — proving the UI is
    # echoing whatever Session() picked, not hardcoding "sdk".
    with client_factory("cli") as c:
        post_body = c.post("/api/sessions").json()
        assert post_body["backend_kind"] == "cli"
        get_body = c.get(f"/api/sessions/{post_body['sid']}").json()
        assert get_body["backend_kind"] == "cli"


def test_delete_session_204_then_404(client):
    sid = client.post("/api/sessions").json()["sid"]
    r1 = client.delete(f"/api/sessions/{sid}")
    assert r1.status_code == 204
    r2 = client.delete(f"/api/sessions/{sid}")
    assert r2.status_code == 404


def test_get_session_snapshot_keys(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {
        "sid",
        "model",
        "n_messages",
        "active_composition",
        "resources",
    }
    assert body["sid"] == sid
    assert body["n_messages"] == 0
    assert body["active_composition"] is None
    assert body["resources"] == []


def test_get_session_404_for_unknown_sid(client):
    r = client.get("/api/sessions/no-such-sid")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# chat (SSE)
# ---------------------------------------------------------------------------


def test_chat_streams_sse_events(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "hi"})
    assert r.status_code == 200
    text = r.text
    assert "event: text_chunk" in text
    assert "event: tool_use" in text
    assert "event: tool_result" in text
    assert "event: turn_complete" in text
    # Final terminator so the JS client knows to stop reading.
    assert "event: end" in text


def test_chat_404_for_unknown_sid(client):
    r = client.post("/api/sessions/no-such-sid/chat", json={"message": "hi"})
    assert r.status_code == 404


def test_chat_updates_active_composition_from_tool_use(client):
    sid = client.post("/api/sessions").json()["sid"]
    # FakeSession.send_user_message yields a tool_use whose input is
    # {"composition": "h_demo"} — after the chat, snapshot should
    # show that as the active composition.
    client.post(f"/api/sessions/{sid}/chat", json={"message": "build a model"})
    snap = client.get(f"/api/sessions/{sid}").json()
    assert snap["active_composition"] == "h_demo"


def test_scan_for_composition_handle_prefers_explicit_composition_arg():
    """Regression: ``add_linear_pathway`` etc. carry node handles in a
    ``nodes`` list alongside the composition ref. The old "last hit
    wins" scan would happily pick a node handle as the active
    composition, breaking every subsequent ``render_composition_graph``
    call with "h_X is not a Composition handle"."""
    from psyneulink_ui.routes import _scan_for_composition_handle

    payload = {
        "composition": "h_demo",
        "nodes": ["h_input_layer", "h_output_layer"],
    }
    assert _scan_for_composition_handle(payload) == "h_demo"


def test_scan_for_composition_handle_ignores_lone_h_in_lists():
    """A bare list of node handles (no composition arg) must NOT be
    interpreted as composition refs."""
    from psyneulink_ui.routes import _scan_for_composition_handle

    assert _scan_for_composition_handle(["h_a", "h_b"]) is None
    assert _scan_for_composition_handle({"nodes": ["h_a", "h_b"]}) is None


def test_composition_handle_from_result_picks_up_create_composition():
    """``create_composition`` is the only place a brand-new composition
    handle exists *only* in the result payload — the input has no
    composition arg to scan. Make sure the result-side helper finds
    it for both transport shapes."""
    from psyneulink_ui.routes import _composition_handle_from_result

    raw_str = json.dumps(
        {"handle": "h_new", "type": "Composition", "name": "demo"}
    )
    assert _composition_handle_from_result(raw_str) == "h_new"

    raw_blocks = [{"type": "text", "text": raw_str}]
    assert _composition_handle_from_result(raw_blocks) == "h_new"


def test_composition_handle_from_result_ignores_non_composition_results():
    """``create_transfer_mechanism`` returns a handle too — but
    ``type=="TransferMechanism"`` so it must NOT be promoted to active
    composition."""
    from psyneulink_ui.routes import _composition_handle_from_result

    mech = json.dumps(
        {"handle": "h_mech", "type": "TransferMechanism", "name": "input"}
    )
    assert _composition_handle_from_result(mech) is None
    assert _composition_handle_from_result("not even json") is None
    assert _composition_handle_from_result(None) is None


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def test_graph_endpoint_calls_render_tool(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps(
            {
                "composition": "h_demo",
                "revision": 3,
                "format": "png",
                "mime": "image/png",
                "data_url": "data:image/png;base64,AAAA",
                "n_nodes": 4,
                "n_projections": 5,
            }
        )
    )
    r = client.get(f"/api/sessions/{sid}/graph?composition=h_demo&fmt=png")
    assert r.status_code == 200
    body = r.json()
    assert body["composition"] == "h_demo"
    assert body["revision"] == 3
    assert body["data_url"].startswith("data:image/png;base64,")
    assert body["n_nodes"] == 4
    ui.session.call_tool.assert_awaited_once_with(
        "render_composition_graph",
        {"composition": "h_demo", "fmt": "png"},
    )


def test_graph_endpoint_returns_204_when_no_active_composition(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = client.get(f"/api/sessions/{sid}/graph")
    assert r.status_code == 204


def test_graph_endpoint_falls_back_to_active_composition(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.active_composition = "h_xyz"
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps(
            {"composition": "h_xyz", "revision": 1, "data_url": "data:image/png;base64,AAA"}
        )
    )
    r = client.get(f"/api/sessions/{sid}/graph")
    assert r.status_code == 200
    ui.session.call_tool.assert_awaited_once_with(
        "render_composition_graph",
        {"composition": "h_xyz", "fmt": "png"},
    )


def test_graph_revision_endpoint_calls_get_revision_tool(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps({"composition": "h_demo", "revision": 7})
    )
    r = client.get(f"/api/sessions/{sid}/graph/revision?composition=h_demo")
    assert r.status_code == 200
    body = r.json()
    assert body["composition"] == "h_demo"
    assert body["revision"] == 7
    ui.session.call_tool.assert_awaited_once_with(
        "get_composition_revision",
        {"composition": "h_demo"},
    )
    # Side-effect: the UI session should remember the revision.
    assert ui.last_revision == 7


def test_graph_revision_endpoint_returns_zero_when_no_active_composition(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = client.get(f"/api/sessions/{sid}/graph/revision")
    assert r.status_code == 200
    body = r.json()
    assert body["composition"] is None
    assert body["revision"] == 0


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------


def _upload(client, sid, kind, filename, payload, content_type):
    return client.post(
        f"/api/sessions/{sid}/resources/{kind}",
        files={"file": (filename, payload, content_type)},
    )


def test_resources_pdf_upload_attaches_resource(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)

    pdf_bytes = b"%PDF-1.4\n%fake bytes for testing\n%%EOF\n"
    r = _upload(client, sid, "pdf", "paper.pdf", pdf_bytes, "application/pdf")

    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "pdf"
    assert body["label"] == "paper.pdf"
    assert body["index"] == 0

    ui.session.attach.assert_called_once()
    attached = ui.session.attach.call_args.args[0]
    from psyneulink_agent.core import PdfResource
    assert isinstance(attached, PdfResource)


def test_resources_data_upload_attaches_resource(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)

    csv_bytes = b"subject_id,trial_global,step\n1,1,1\n"
    r = _upload(client, sid, "data", "obs.csv", csv_bytes, "text/csv")

    assert r.status_code == 200
    assert r.json()["kind"] == "data"
    from psyneulink_agent.core import DataResource
    assert isinstance(ui.session.attach.call_args.args[0], DataResource)


def test_resources_model_upload_attaches_resource(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)

    py_bytes = b"# model.py\nimport psyneulink as pnl\n"
    r = _upload(client, sid, "model", "model.py", py_bytes, "text/x-python")

    assert r.status_code == 200
    assert r.json()["kind"] == "model"
    from psyneulink_agent.core import ModelFileResource
    assert isinstance(ui.session.attach.call_args.args[0], ModelFileResource)


def test_resources_unknown_kind_returns_400(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = _upload(
        client, sid, "scribble", "foo.txt", b"hello", "text/plain"
    )
    assert r.status_code == 400


def test_resources_pdf_upload_with_wrong_extension_returns_400(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = _upload(
        client, sid, "pdf", "not-a-pdf.txt", b"hello", "text/plain"
    )
    assert r.status_code == 400


def test_resources_list(client):
    sid = client.post("/api/sessions").json()["sid"]
    _upload(client, sid, "pdf", "a.pdf", b"%PDF-1.4\n", "application/pdf")
    _upload(client, sid, "data", "b.csv", b"x\n1\n", "text/csv")

    r = client.get(f"/api/sessions/{sid}/resources")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert items[0]["kind"] == "pdf"
    assert items[1]["kind"] == "data"
    assert items[0]["index"] == 0 and items[1]["index"] == 1


def test_resources_delete_by_index(client):
    sid = client.post("/api/sessions").json()["sid"]
    _upload(client, sid, "pdf", "a.pdf", b"%PDF-1.4\n", "application/pdf")
    ui = state.REGISTRY.get(sid)

    r = client.delete(f"/api/sessions/{sid}/resources/0")
    assert r.status_code == 204
    ui.session.detach.assert_called_once()

    # Out-of-range now.
    r2 = client.delete(f"/api/sessions/{sid}/resources/0")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# model save / load
# ---------------------------------------------------------------------------


def test_save_model_calls_export_python_script_tool(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.active_composition = "h_demo"
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps({"path": "/tmp/m.py", "ok": True})
    )

    r = client.post(
        f"/api/sessions/{sid}/model/save",
        json={"path": "/tmp/m.py"},
    )
    assert r.status_code == 200
    assert r.json() == {"path": "/tmp/m.py", "ok": True}
    ui.session.call_tool.assert_awaited_once_with(
        "export_python_script",
        {"path": "/tmp/m.py", "composition": "h_demo"},
    )


def test_save_model_omits_composition_when_none(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.session.call_tool = AsyncMock(return_value="{}")
    client.post(
        f"/api/sessions/{sid}/model/save",
        json={"path": "/tmp/m.py"},
    )
    ui.session.call_tool.assert_awaited_once_with(
        "export_python_script",
        {"path": "/tmp/m.py"},
    )


def test_save_model_400_when_path_missing(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = client.post(f"/api/sessions/{sid}/model/save", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_endpoint_signals_session_when_turn_in_flight(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    # Simulate a turn in flight — Session normally creates/clears this
    # in send_user_message; tests poke it directly.
    import asyncio

    ui.session._cancel_event = asyncio.Event()

    r = client.post(f"/api/sessions/{sid}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body == {"cancelled": True}
    ui.session.cancel_current_turn.assert_called_once()
    assert ui.session._cancel_event.is_set()


def test_cancel_endpoint_returns_no_active_turn_when_idle(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    assert ui.session._cancel_event is None  # nothing in flight

    r = client.post(f"/api/sessions/{sid}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is False
    assert "no active turn" in body["reason"]


def test_cancel_endpoint_404_for_unknown_sid(client):
    r = client.post("/api/sessions/no-such-sid/cancel")
    assert r.status_code == 404


def test_chat_emits_turn_cancelled_event_when_backend_yields_one(client):
    """The agent backend's ``turn_cancelled`` must surface as its own
    SSE event — that's what the JS handler keys off to paint the
    ``Stopped by user.`` marker in the scrollback."""
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)

    async def _gen(text, *, anthropic_client=None):
        ui.session.history.append({"role": "user", "content": text})
        yield {"type": "text_chunk", "text": "starting…"}
        yield {"type": "turn_cancelled"}
        # If the SSE handler honours turn_cancelled it should NOT see
        # this final event — the loop must break first.
        yield {"type": "text_chunk", "text": "should-not-appear"}

    ui.session.send_user_message = _gen

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "long task"})
    assert r.status_code == 200
    text = r.text
    assert "event: text_chunk" in text
    assert "event: turn_cancelled" in text
    assert "event: end" in text
    assert "should-not-appear" not in text


# ---------------------------------------------------------------------------
# code (live preview pane)
# ---------------------------------------------------------------------------


def test_code_endpoint_calls_export_python_script_with_dry_run(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.active_composition = "h_demo"
    ui.last_revision = 7
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps(
            {
                "path": None,
                "text": "import psyneulink as pnl\n# ...stub...\n",
                "n_objects": 3,
                "n_operations": 1,
            }
        )
    )

    r = client.get(f"/api/sessions/{sid}/code")
    assert r.status_code == 200
    body = r.json()
    assert body["composition"] == "h_demo"
    assert body["revision"] == 7
    assert "import psyneulink as pnl" in body["text"]
    assert body["n_objects"] == 3
    assert body["n_operations"] == 1
    # The MCP-side coupling: dry_run=True keeps this poll cheap and
    # disk-quiet. If this argv shape ever drifts, every poll will start
    # writing a .py file — see the cross-repo coupling note in
    # ``persistence.py``.
    ui.session.call_tool.assert_awaited_once_with(
        "export_python_script",
        {"composition": "h_demo", "dry_run": True},
    )


def test_code_endpoint_returns_204_when_no_active_composition(client):
    sid = client.post("/api/sessions").json()["sid"]
    r = client.get(f"/api/sessions/{sid}/code")
    assert r.status_code == 204


def test_code_endpoint_falls_back_to_active_composition(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.active_composition = "h_active"
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps({"path": None, "text": "src", "n_objects": 1})
    )
    r = client.get(f"/api/sessions/{sid}/code")
    assert r.status_code == 200
    ui.session.call_tool.assert_awaited_once_with(
        "export_python_script",
        {"composition": "h_active", "dry_run": True},
    )


def test_code_endpoint_explicit_composition_query_param_wins(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.active_composition = "h_active"
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps({"path": None, "text": "src", "n_objects": 0})
    )
    r = client.get(f"/api/sessions/{sid}/code?composition=h_other")
    assert r.status_code == 200
    ui.session.call_tool.assert_awaited_once_with(
        "export_python_script",
        {"composition": "h_other", "dry_run": True},
    )


def test_code_endpoint_404_for_unknown_sid(client):
    r = client.get("/api/sessions/no-such-sid/code")
    assert r.status_code == 404


def test_load_model_calls_load_python_script_tool(client):
    sid = client.post("/api/sessions").json()["sid"]
    ui = state.REGISTRY.get(sid)
    ui.session.call_tool = AsyncMock(
        return_value=json.dumps({"loaded": "h_loaded"})
    )

    r = client.post(
        f"/api/sessions/{sid}/model/load",
        json={"path": "/tmp/m.py"},
    )
    assert r.status_code == 200
    assert r.json() == {"loaded": "h_loaded"}
    ui.session.call_tool.assert_awaited_once_with(
        "load_python_script", {"path": "/tmp/m.py"}
    )
