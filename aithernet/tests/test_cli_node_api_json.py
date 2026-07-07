"""CLI robustness: node-API responses that are not JSON fail cleanly (beta.5 Area 5).

When something other than the Aithernet node API answers the node URL (e.g. another service bound
to the node's port returns HTML), the CLI must report a structured, truthful readiness error rather
than crashing with an unhandled ``JSONDecodeError`` traceback.
"""

from __future__ import annotations

import httpx
import pytest
import typer

from aithernet import cli


def _html_response(path: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html"},
        text="<!doctype html><html><title>Some Other App</title></html>",
        request=httpx.Request("GET", f"http://127.0.0.1:8080{path}"),
    )


def test_json_helper_raises_clean_exit_on_html(capsys):
    with pytest.raises(typer.Exit) as exc:
        cli._json(_html_response("/mcp/diagnostics?probe=true"))
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "non-JSON response" in err
    assert "aithernet service start" in err
    assert "Traceback" not in err  # no unhandled crash
    assert "<html>" not in err  # raw HTML never dumped


def test_json_helper_passes_through_valid_json():
    resp = httpx.Response(
        200,
        json={"configured": False, "provider": "stdio"},
        request=httpx.Request("GET", "http://127.0.0.1:8080/mcp/status"),
    )
    assert cli._json(resp) == {"configured": False, "provider": "stdio"}
