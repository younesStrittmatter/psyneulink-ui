"""Tests for the tiny SSE encoder helpers."""

from __future__ import annotations

import json

from psyneulink_ui.sse import sse_comment, sse_event


def test_sse_event_shape():
    out = sse_event("text_chunk", {"text": "hello"})
    assert out.startswith("event: text_chunk\n")
    assert "data: " in out
    assert out.endswith("\n\n")


def test_sse_event_json_encodes_data():
    out = sse_event("tool_use", {"name": "x", "input": {"k": 1}})
    data_line = [
        line for line in out.split("\n") if line.startswith("data: ")
    ][0]
    payload = json.loads(data_line[len("data: "):])
    assert payload == {"name": "x", "input": {"k": 1}}


def test_sse_event_falls_back_for_non_json_serialisable():
    class _Weird:
        def __str__(self) -> str:
            return "weird"

    out = sse_event("x", {"v": _Weird()})
    data_line = [line for line in out.split("\n") if line.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    assert payload == {"v": "weird"}


def test_sse_comment_shape():
    out = sse_comment("ping")
    assert out == ": ping\n\n"
