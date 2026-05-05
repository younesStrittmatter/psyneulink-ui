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
* **Cancel** (``POST /api/sessions/{sid}/cancel``) — interrupt the
  currently-streaming chat turn. Signal-only: returns 200 immediately
  with ``{"cancelled": <bool>}``; the SSE stream itself emits a
  ``turn_cancelled`` event before its final ``end``.
* **Graph** (``GET /api/sessions/{sid}/graph``,
  ``GET /api/sessions/{sid}/graph/revision``) — wraps the MCP tools
  ``render_composition_graph`` and ``get_composition_revision``.
* **Code** (``GET /api/sessions/{sid}/code``) — live-preview pane
  fed by ``export_python_script(dry_run=True)``. **Coupling note**:
  this endpoint depends on the ``dry_run`` flag added to
  ``export_python_script`` in ``psyneulink-mcp``. If the MCP ever
  drops it (or renames it), this pane goes blank — keep them in
  lockstep.
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


def _composition_handle_from_result(content: Any) -> str | None:
    """Extract a Composition handle from a tool_result payload, if any.

    MCP curated tools return a JSON object like
    ``{"handle": "h_...", "type": "Composition", "name": "..."}``. The
    transport may surface this as either a string (SDK backend) or a
    list of text blocks (CLI backend's stream-json shape) — handle
    both. Anything else (non-Composition objects, malformed strings,
    etc.) returns None and the caller leaves ``active_composition``
    untouched.
    """
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                inner = item.get("text", "")
                if isinstance(inner, str):
                    parts.append(inner)
        text = "\n".join(parts) if parts else None
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") != "Composition":
        return None
    handle = parsed.get("handle")
    if isinstance(handle, str) and handle.startswith(COMPOSITION_HANDLE_PREFIX):
        return handle
    return None


def _scan_for_composition_handle(value: Any) -> str | None:
    """Walk a tool_use ``input`` payload looking for a composition handle.

    The MCP convention is that composition handles are short strings
    starting with the ``h_`` prefix and that every curated tool which
    takes a composition exposes it under a top-level key literally
    named ``composition`` (see ``psyneulink_mcp/tools/curated/*.py``).

    We honour that convention strictly: when a dict has a
    ``composition`` key, that value wins. Falling back to "scan every
    ``h_`` we can find, last hit wins" was wrong — for tools like
    ``add_linear_pathway`` whose input is
    ``{composition: h_demo, nodes: [h_input, h_output]}`` it would
    happily promote ``h_output`` to active composition and break every
    subsequent ``render_composition_graph`` call.
    """
    if isinstance(value, str):
        if value.startswith(COMPOSITION_HANDLE_PREFIX):
            return value
        return None
    if isinstance(value, dict):
        if "composition" in value:
            hit = _scan_for_composition_handle(value["composition"])
            if hit is not None:
                return hit
        # No explicit composition arg: recurse but only into nested
        # dicts (not lists, which usually carry node refs). This still
        # catches future tool shapes that nest composition handles
        # inside grouping dicts.
        found: str | None = None
        for k, v in value.items():
            if k == "composition":
                continue
            if isinstance(v, dict):
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
        # Which LLM backend ``Session`` picked (``"sdk"`` / ``"cli"`` /
        # ``"unknown"``). Lets the frontend show ``backend: …`` in the
        # status line without an extra round-trip.
        "backend_kind": ui.backend_kind,
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
        # Mirror of the field returned by ``POST /sessions`` so a
        # browser refresh (which only does GET) still knows which LLM
        # backend is in use.
        "backend_kind": ui.backend_kind,
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
                    # ``create_composition`` is the only place a brand-
                    # new composition handle exists *only* in the
                    # result payload (the input had no composition arg
                    # to scan). Pluck it out here so the UI's graph
                    # endpoints have something to render even if the
                    # turn ends right after creation.
                    if not ev.get("is_error"):
                        new_handle = _composition_handle_from_result(
                            ev.get("content", "")
                        )
                        if new_handle is not None:
                            ui.active_composition = new_handle
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
                elif etype == "turn_cancelled":
                    # Backend honoured a Stop button press. Surface it
                    # as a first-class event so the JS handler can
                    # paint a cancelled marker in the scrollback
                    # before the stream's terminal ``end`` arrives.
                    yield sse_event("turn_cancelled", {})
                    break
                else:
                    # Forward anything else verbatim so we don't
                    # silently drop new event types added later.
                    yield sse_event(etype or "unknown", ev)

                # Belt-and-suspenders: even if the backend hasn't yet
                # surfaced ``turn_cancelled``, observe the Session-level
                # cancel flag between events and bail. Keeps the stream
                # responsive when a backend is slow to react.
                cancel_ev = getattr(ui.session, "_cancel_event", None)
                if cancel_ev is not None and cancel_ev.is_set():
                    yield sse_event("turn_cancelled", {})
                    break
        except Exception as exc:  # noqa: BLE001 — surface to client, never crash the stream
            log.exception("chat stream failed")
            yield sse_event(
                "error",
                {"message": f"{type(exc).__name__}: {exc}"},
            )
        finally:
            yield sse_event("end", {})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/sessions/{sid}/cancel")
async def cancel_chat(sid: str) -> dict[str, Any]:
    """Signal-only: ask the active chat turn to stop.

    Returns immediately with ``{"cancelled": True}`` if a turn was in
    flight (the SSE stream will then emit ``turn_cancelled`` followed
    by ``end``), or ``{"cancelled": False, "reason": "no active turn"}``
    if nothing was running. Never blocks waiting for the stream to
    actually wind down — the front-end fires this and forgets, then
    waits for the SSE side to close.
    """
    ui = _require(sid)
    cancel_method = getattr(ui.session, "cancel_current_turn", None)
    if cancel_method is None:
        # Older agent install without the cancel hook. Don't 500 —
        # report it the same way as "no active turn" so the UI
        # gracefully no-ops.
        return {"cancelled": False, "reason": "session has no cancel hook"}
    cancelled = bool(cancel_method())
    if not cancelled:
        return {"cancelled": False, "reason": "no active turn"}
    return {"cancelled": True}


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
# code preview pane
# ---------------------------------------------------------------------------


@router.get("/sessions/{sid}/code")
async def code(sid: str, composition: str | None = None) -> Response:
    """Return the current composition rendered as a Python script.

    Calls the MCP's ``export_python_script`` with ``dry_run=True`` so
    no file is written — this is a live preview, not a save. Polled
    by the frontend on the same revision tick that drives the graph
    pane (no separate timer). 204 when there's no active composition,
    same as ``GET /graph``.

    **Cross-repo coupling**: the ``dry_run`` flag is defined in
    ``psyneulink-mcp/src/psyneulink_mcp/tools/curated/persistence.py``.
    If that argument disappears or changes name, this endpoint silently
    starts writing files on every poll — keep them in sync.
    """
    ui = _require(sid)
    handle = composition or ui.active_composition
    if handle is None:
        return Response(status_code=204)
    raw = await ui.session.call_tool(
        "export_python_script",
        {"composition": handle, "dry_run": True},
    )
    parsed = _parse_tool_json(raw)
    text = parsed.get("text", "")
    if not isinstance(text, str):
        text = str(text)
    payload = {
        "composition": handle,
        "revision": ui.last_revision,
        "text": text,
        "n_objects": parsed.get("n_objects"),
        "n_operations": parsed.get("n_operations"),
        "error": parsed.get("error"),
    }
    return Response(content=json.dumps(payload), media_type="application/json")


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
