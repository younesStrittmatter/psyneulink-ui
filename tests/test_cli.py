"""Tests for the ``psyneulink-ui`` CLI."""

from __future__ import annotations

from psyneulink_ui import cli


def test_build_parser_defaults():
    ns = cli.build_parser().parse_args([])
    assert ns.host == "127.0.0.1"
    assert ns.port == 8000
    assert ns.reload is False
    assert ns.log_level == "info"


def test_build_parser_overrides():
    ns = cli.build_parser().parse_args(
        ["--host", "0.0.0.0", "--port", "9999", "--reload", "--log-level", "debug"]
    )
    assert ns.host == "0.0.0.0"
    assert ns.port == 9999
    assert ns.reload is True
    assert ns.log_level == "debug"


def test_main_invokes_uvicorn_run(monkeypatch):
    captured = {}

    def fake_run(target, **kwargs):
        captured["target"] = target
        captured.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    rc = cli.main(["--host", "127.0.0.1", "--port", "8123"])
    assert rc == 0
    assert captured["target"] == "psyneulink_ui.server:app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123
    assert captured["reload"] is False
    assert captured["log_level"] == "info"
