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
