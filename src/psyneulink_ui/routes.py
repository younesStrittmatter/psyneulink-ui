"""HTTP API surface for the UI.

Every endpoint here is a thin translation of an HTTP request into
exactly one or two calls into agent core (``Session.send_user_message``,
``Session.call_tool``, ``Session.attach``, ``Session.detach``). No
modeling logic lives here.

Endpoint families:

* **Session lifecycle** (``POST /api/sessions``,
  ``DELETE /api/sessions/{sid}``, ``GET /api/sessions/{sid}``).
* **Chat** (``POST /api/sessions/{sid}/chat``) — streams events from
  ``Session.send_user_message`` as Server-Sent Events.
* **Graph** (``GET /api/sessions/{sid}/graph``,
  ``GET /api/sessions/{sid}/graph/revision``) — wraps the MCP tools
  ``render_composition_graph`` and ``get_composition_revision``.
* **Resources** (``POST /api/sessions/{sid}/resources/{kind}``,
  ``GET /api/sessions/{sid}/resources``,
  ``DELETE /api/sessions/{sid}/resources/{index}``).
* **Model save / load** (``POST /api/sessions/{sid}/model/save``,
  ``POST /api/sessions/{sid}/model/load``) — wraps the existing
  persistence MCP tools.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from psyneulink_agent.core import (
    DataResource,
    ModelFileResource,
    PdfResource,
    Resource,
)

from .sse import sse_event
from .state import COMPOSITION_HANDLE_PREFIX, REGISTRY, UISession, list_tools_for_ui

log = logging.getLogger("psyneulink_ui")

router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require(sid: str) -> UISession:
    ui = REGISTRY.get(sid)
    if ui is None:
        raise HTTPException(status_code=404, detail=f"unknown session: {sid}")
    return ui


def _scan_for_composition_handle(value: Any) -> str | None:
    """Walk a tool_use ``input`` payload looking for a composition handle.

    The MCP convention is that composition handles are short strings
    starting with the ``h_`` prefix. We scan recursively because some
    tools nest composition refs inside dicts/lists. Last hit wins;
    callers use this to update ``UISession.active_composition``.
    """
    if isinstance(value, str):
        if value.startswith(COMPOSITION_HANDLE_PREFIX):
            return value
        return None
    if isinstance(value, dict):
        found: str | None = None
        for v in value.values():
            hit = _scan_for_composition_handle(v)
            if hit is not None:
                found = hit
        return found
    if isinstance(value, (list, tuple)):
        found = None
        for v in value:
            hit = _scan_for_composition_handle(v)
            if hit is not None:
                found = hit
        return found
    return None


def _parse_tool_json(raw: str) -> dict[str, Any]:
    """Parse the flat-string return value of an MCP tool into a dict.

    MCP flattens tool results into a single string — for tools that
    return JSON we get the JSON encoded as text, so the UI has to
    parse it. If the tool returned non-JSON (e.g. an error sentinel),
    we surface the raw string under an ``error`` key rather than
    throwing — the UI can then show the raw text to the user.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "tool returned non-JSON content", "raw": raw}
    if not isinstance(parsed, dict):
        return {"value": parsed}
    return parsed


# ---------------------------------------------------------------------------
# session lifecycle
# ---------------------------------------------------------------------------


@router.post("/sessions")
async def create_session() -> dict[str, Any]:
    ui = await REGISTRY.create()
    prompt = ui.session.system_prompt()
    return {
        "sid": ui.sid,
        "model": ui.session.model,
        "system_prompt_preview": prompt[:200],
    }


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(sid: str) -> Response:
    ok = await REGISTRY.close(sid)
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown session: {sid}")
    return Response(status_code=204)


@router.get("/sessions/{sid}")
async def get_session(sid: str) -> dict[str, Any]:
    ui = _require(sid)
    snap = ui.session.snapshot()
    return {
        "sid": sid,
        "model": snap.get("model"),
        "n_messages": len(ui.session.history),
        "active_composition": ui.active_composition,
        "last_revision": ui.last_revision,
        "resources": snap.get("resources", []),
    }


# ---------------------------------------------------------------------------
# chat (SSE)
# ---------------------------------------------------------------------------


@router.post("/sessions/{sid}/chat")
async def chat(sid: str, request: Request) -> StreamingResponse:
    ui = _require(sid)
    body = await request.json()
    message = body.get("message", "")

    async def event_stream():
        try:
            async for ev in ui.session.send_user_message(message):
                etype = ev.get("type")
                if etype == "text_chunk":
                    # Accept either ``delta`` (real loop) or ``text``
                    # (test-fakes); normalise to ``text`` on the wire so
                    # the JS handler stays simple.
                    text = ev.get("delta") or ev.get("text") or ""
                    yield sse_event("text_chunk", {"text": text})
                elif etype == "tool_use":
                    handle = _scan_for_composition_handle(ev.get("input"))
                    if handle is not None:
                        ui.active_composition = handle
                    yield sse_event(
                        "tool_use",
                        {
                            "id": ev.get("id"),
                            "name": ev.get("name"),
                            "input": ev.get("input", {}),
                        },
                    )
                elif etype == "tool_result":
                    yield sse_event(
                        "tool_result",
                        {
                            "id": ev.get("id"),
                            "name": ev.get("name"),
                            "content": ev.get("content", ""),
                            "is_error": bool(ev.get("is_error", False)),
                        },
                    )
                elif etype == "turn_complete":
                    yield sse_event(
                        "turn_complete",
                        {"reason": ev.get("stop_reason")},
                    )
                else:
                    # Forward anything else verbatim so we don't
                    # silently drop new event types added later.
                    yield sse_event(etype or "unknown", ev)
        except Exception as exc:  # noqa: BLE001 — surface to client, never crash the stream
            log.exception("chat stream failed")
            yield sse_event(
                "error",
                {"message": f"{type(exc).__name__}: {exc}"},
            )
        finally:
            yield sse_event("end", {})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


@router.get("/sessions/{sid}/graph")
async def graph(sid: str, composition: str | None = None, fmt: str = "png") -> Response:
    ui = _require(sid)
    handle = composition or ui.active_composition
    if handle is None:
        # No composition has been touched yet; nothing to render.
        return Response(status_code=204)
    raw = await ui.session.call_tool(
        "render_composition_graph",
        {"composition": handle, "fmt": fmt},
    )
    parsed = _parse_tool_json(raw)
    if "revision" in parsed and isinstance(parsed["revision"], int):
        ui.last_revision = parsed["revision"]
    return Response(content=json.dumps(parsed), media_type="application/json")


@router.get("/sessions/{sid}/graph/revision")
async def graph_revision(sid: str, composition: str | None = None) -> dict[str, Any]:
    ui = _require(sid)
    handle = composition or ui.active_composition
    if handle is None:
        return {"composition": None, "revision": 0}
    raw = await ui.session.call_tool(
        "get_composition_revision",
        {"composition": handle},
    )
    parsed = _parse_tool_json(raw)
    if "revision" in parsed and isinstance(parsed["revision"], int):
        ui.last_revision = parsed["revision"]
    return parsed


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------

_RESOURCE_KINDS = {
    "pdf": PdfResource,
    "data": DataResource,
    "model": ModelFileResource,
}


@router.post("/sessions/{sid}/resources/{kind}")
async def upload_resource(
    sid: str,
    kind: str,
    file: Annotated[UploadFile, File(...)],
) -> dict[str, Any]:
    ui = _require(sid)
    cls = _RESOURCE_KINDS.get(kind)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"unknown resource kind: {kind!r}")

    # Persist the upload to the session's tempdir under its original
    # filename. Resource subclasses re-validate the path themselves
    # (e.g. PdfResource refuses non-.pdf, ModelFileResource refuses
    # non-.py), so a 4xx here is a real client-side mistake.
    upload_dir = ui.upload_dir()
    safe_name = (file.filename or "upload").replace("/", "_").replace("\\", "_") or "upload"
    dest = upload_dir / safe_name
    payload = await file.read()
    dest.write_bytes(payload)

    try:
        resource: Resource = cls(dest)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ui.session.attach(resource)
    index = len(ui.session.resources) - 1
    return {
        "index": index,
        "kind": resource.kind(),
        "label": resource.label(),
        "path": str(dest),
    }


@router.get("/sessions/{sid}/resources")
async def list_resources(sid: str) -> list[dict[str, Any]]:
    ui = _require(sid)
    return [
        {"index": i, "kind": r.kind(), "label": r.label()}
        for i, r in enumerate(ui.session.resources)
    ]


@router.delete("/sessions/{sid}/resources/{index}", status_code=204)
async def delete_resource(sid: str, index: int) -> Response:
    ui = _require(sid)
    if not (0 <= index < len(ui.session.resources)):
        raise HTTPException(status_code=404, detail=f"no resource at index {index}")
    resource = ui.session.resources[index]
    ui.session.detach(resource)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# tools introspection
# ---------------------------------------------------------------------------


@router.get("/sessions/{sid}/tools")
async def list_tools(sid: str) -> list[dict[str, Any]]:
    ui = _require(sid)
    try:
        return await list_tools_for_ui(ui.session)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# model save / load (existing MCP tools, surfaced to the UI dock)
# ---------------------------------------------------------------------------


@router.post("/sessions/{sid}/model/save")
async def save_model(sid: str, request: Request) -> dict[str, Any]:
    ui = _require(sid)
    body = await request.json()
    path = body.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="missing 'path'")
    args: dict[str, Any] = {"path": path}
    if ui.active_composition is not None:
        args["composition"] = ui.active_composition
    raw = await ui.session.call_tool("export_python_script", args)
    return _parse_tool_json(raw)


@router.post("/sessions/{sid}/model/load")
async def load_model(sid: str, request: Request) -> dict[str, Any]:
    ui = _require(sid)
    body = await request.json()
    path = body.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="missing 'path'")
    raw = await ui.session.call_tool("load_python_script", {"path": path})
    return _parse_tool_json(raw)
