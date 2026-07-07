"""CLI tests for the customer-facing `--version` option and the `start` lifecycle (beta.6 fixes).

`aithernet --version` must report the installed package version without loading config, contacting
a node, or reading developer state. `aithernet start` must refuse a second invocation with a
concise, customer-safe message (not an EADDRINUSE traceback) when the port is already in use.
"""

from __future__ import annotations

import socket

from typer.testing import CliRunner

from aithernet.cli import app

_runner = CliRunner()


def test_version_option_reports_installed_version() -> None:
    result = _runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("aithernet ")
    assert "No such option" not in result.output


def test_version_short_flag_works() -> None:
    result = _runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert result.output.strip().startswith("aithernet ")


def test_version_does_not_load_config_or_contact_node(monkeypatch) -> None:
    # If --version tried to load configuration or reach the node, these would be called. The eager
    # option must short-circuit before any of that.
    import aithernet.cli as cli

    monkeypatch.setattr(cli, "load_config", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("--version must not load config")))
    result = _runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip().startswith("aithernet ")


def test_start_refuses_when_port_already_in_use(monkeypatch) -> None:
    # Bind a real socket so the port is genuinely occupied, then point `start` at it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        result = _runner.invoke(app, ["start", "--host", "127.0.0.1", "--port", str(busy_port)])
    finally:
        sock.close()
    # Concise, customer-safe message + non-zero exit — NOT a traceback / EADDRINUSE.
    assert result.exit_code == 1, result.output
    assert "already appears to be running" in result.output
    assert "aithernet status" in result.output
    assert "Errno 98" not in result.output
    assert "Traceback" not in result.output


def test_port_in_use_helper_detects_listener() -> None:
    from aithernet.cli import _port_in_use

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert _port_in_use("127.0.0.1", port) is True
    finally:
        sock.close()
    # After close, the port is free again.
    assert _port_in_use("127.0.0.1", port) is False
