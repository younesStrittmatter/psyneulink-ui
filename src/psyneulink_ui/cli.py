"""``psyneulink-ui`` CLI: thin uvicorn wrapper.

Kept deliberately tiny — there is one entrypoint (the FastAPI app at
``psyneulink_ui.server:app``) and a few uvicorn flags. No subcommands,
no config file. Add complexity only when a real second user shows up.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="psyneulink-ui",
        description="Run the psyneulink-ui web app on localhost.",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code changes (dev).",
    )
    p.add_argument("--log-level", default="info")
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    uvicorn.run(
        "psyneulink_ui.server:app",
        host=ns.host,
        port=ns.port,
        reload=ns.reload,
        log_level=ns.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
