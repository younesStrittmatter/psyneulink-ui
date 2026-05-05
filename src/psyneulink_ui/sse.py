"""Server-Sent Events encoders.

Tiny helpers for formatting the `text/event-stream` wire format. Pulled
out of `routes.py` so route handlers can stay focused on the agent-core
glue and so the encoders are unit-testable in isolation.
"""

from __future__ import annotations

import json
from typing import Any


def sse_event(event_type: str, data: dict[str, Any]) -> str:
    """Format one SSE message frame.

    Always JSON-encodes the data block so the browser-side parser only
    has to do a single ``JSON.parse`` per event. Non-JSON-serialisable
    values fall back to ``str(...)`` rather than raising — events are
    diagnostic, not load-bearing data.
    """
    payload = json.dumps(data, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


def sse_comment(text: str) -> str:
    """Format an SSE comment line (used as keep-alive / debug breadcrumb)."""
    return f": {text}\n\n"
