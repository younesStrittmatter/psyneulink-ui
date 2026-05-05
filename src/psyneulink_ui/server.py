"""FastAPI app factory for psyneulink-ui.

Two responsibilities:

* mount the static frontend (``index.html`` + ``app.js`` + ``style.css``)
  at ``/`` and ``/static``;
* mount the JSON / SSE API at ``/api`` (see ``routes.py``).

The per-browser-tab ``Session.lifespan()`` is owned by ``state.py`` —
this module's app-level ``lifespan`` only handles cleanup at process
shutdown (``REGISTRY.close_all``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routes import router
from .state import REGISTRY


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await REGISTRY.close_all()


def create_app() -> FastAPI:
    app = FastAPI(
        title="psyneulink-ui",
        version="0.1.0",
        description="Two-pane web UI for the psyneulink-ai modeling stack.",
        lifespan=lifespan,
    )

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.include_router(router, prefix="/api")
    return app


app = create_app()
