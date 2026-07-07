"""beta.5 Part A — mission HTTP routing/isolation + non-JSON safety.

Regression coverage for the release-blocking bug where ``aithernet mission list`` crashed with a
raw ``JSONDecodeError`` and ``aithernet run`` dumped a raw nginx ``405`` HTML page, because a
foreign web server (a Caddy/nginx reverse proxy) was bound to the node's port while the node was
down. The CLI must instead emit a structured, secret-free error, and must never let the coordinator
provider endpoint be used as the mission API.
"""

from __future__ import annotations

import httpx
import pytest
import typer
from typer.testing import CliRunner

from aithernet import cli
from aithernet.cli import app

_runner = CliRunner()


def _text(result) -> str:
    """Combined stdout+stderr for a CliRunner result (click 8.4 separates them)."""
    parts = [result.output or ""]
    try:
        if result.stderr:
            parts.append(result.stderr)
    except ValueError:
        pass
    return "".join(parts)

_NGINX_405 = (
    "<html>\r\n<head><title>405 Not Allowed</title></head>\r\n<body>\r\n"
    "<center><h1>405 Not Allowed</h1></center>\r\n<hr><center>nginx/1.27.5</center>\r\n"
    "</body>\r\n</html>\r\n"
)
_SPA_HTML = (
    "<!doctype html><html lang=\"en\"><head><title>Aithernet Portal</title></head>"
    "<body><div id=\"root\">loading…</div>"
    "<script>window.__TOKEN__='sk-secret'</script></body></html>"
)


class _FakeCoord:
    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url


class _FakeConfig:
    def __init__(self, base_url: str, coord_url: str | None) -> None:
        self._base_url = base_url
        self.coordinator = _FakeCoord(coord_url)

    @property
    def base_url(self) -> str:
        return self._base_url


def _install(monkeypatch, *, node_url: str, coord_url: str | None, handler):
    """Wire the CLI so mission commands resolve to ``node_url``, the configured coordinator
    endpoint is ``coord_url``, and every HTTP call is served by ``handler`` (which also records
    the (host, port) it was asked for)."""
    monkeypatch.setattr(cli, "_effective_config", lambda c: c)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _FakeConfig(node_url, coord_url))

    calls: list[tuple[str, int, str, str]] = []

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.url.port or 80, request.method, request.url.path))
        return handler(request)

    def _make_client(base_url: str) -> httpx.Client:
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(_recording_handler))

    monkeypatch.setattr(cli, "_client", _make_client)
    return calls


# -- F.2: 405 HTML handling ----------------------------------------------------------------------


def test_run_405_html_is_structured_not_raw(monkeypatch):
    """`aithernet run` against an nginx 405 must not dump the raw HTML page."""
    def handler(request):
        return httpx.Response(405, headers={"content-type": "text/html",
                                            "server": "nginx/1.27.5"}, text=_NGINX_405)

    _install(monkeypatch, node_url="http://127.0.0.1:8080", coord_url="http://127.0.0.1:8000/v1",
             handler=handler)
    result = _runner.invoke(app, ["run", "do a thing"])

    assert result.exit_code == 1
    out = _text(result)
    assert "http status:  405" in out
    assert "content-type: text/html" in out
    assert "mission_api" in out
    # raw HTML must never reach the terminal; only a short sanitized preview survives.
    assert "<html>" not in out
    assert "<center>" not in out
    assert "405 Not Allowed" in out  # tag-stripped preview is fine
    # no crash / traceback / JSONDecodeError leaked
    assert "Traceback" not in out
    assert "JSONDecodeError" not in out


def test_mission_list_200_html_is_structured_not_jsondecodeerror(monkeypatch):
    """The exact reported bug: a 200 text/html SPA index must not raise JSONDecodeError."""
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html", "server": "Caddy"},
                              text=_SPA_HTML)

    _install(monkeypatch, node_url="http://127.0.0.1:8080", coord_url=None, handler=handler)
    result = _runner.invoke(app, ["mission", "list"])

    assert result.exit_code == 1
    out = _text(result)
    assert "non-JSON response" in out
    assert "GET http://127.0.0.1:8080/missions" in out
    assert "Traceback" not in out
    assert "JSONDecodeError" not in out
    # the embedded script secret must never be echoed (script bodies are stripped entirely)
    assert "sk-secret" not in out
    assert "<script>" not in out


# -- F.1: mission endpoint isolation from the coordinator (CatGPT) endpoint -----------------------


def test_mission_list_never_calls_coordinator_endpoint(monkeypatch):
    """With a CatGPT coordinator configured at :8000, `mission list` must call ONLY the node
    (:8080) mission API — never the coordinator/CatGPT endpoint."""
    def handler(request):
        # node answers with a real (empty) mission list
        return httpx.Response(200, json=[])

    calls = _install(monkeypatch, node_url="http://127.0.0.1:8080",
                     coord_url="http://127.0.0.1:8000/v1", handler=handler)
    result = _runner.invoke(app, ["mission", "list"])

    assert result.exit_code == 0
    assert "No missions." in _text(result)
    assert calls, "expected at least one HTTP call"
    for host, port, _method, path in calls:
        assert (host, port) == ("127.0.0.1", 8080), f"leaked call to {host}:{port}"
        assert "/v1/chat/completions" not in path
        assert "/v1/models" not in path


# -- A.3 / F.3: config/routing isolation guardrail -----------------------------------------------


def test_mission_api_equal_to_coordinator_endpoint_is_blocked(monkeypatch):
    """If the resolved mission API base URL IS the coordinator endpoint, block before any call."""
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("no HTTP call must be made when the boundary guard blocks")

    calls = _install(monkeypatch, node_url="http://127.0.0.1:8000",
                     coord_url="http://127.0.0.1:8000/v1", handler=handler)
    result = _runner.invoke(app, ["mission", "list"])

    assert result.exit_code == 1
    assert ("Coordinator provider endpoint cannot be used as the Aithernet mission API"
            in _text(result))
    assert calls == [], "the guard must short-circuit before making any HTTP request"


def test_run_boundary_guard_blocks_before_creating_mission(monkeypatch):
    def handler(request):  # pragma: no cover
        raise AssertionError("run must not POST to the coordinator endpoint")

    calls = _install(monkeypatch, node_url="http://127.0.0.1:8000",
                     coord_url="http://127.0.0.1:8000/v1", handler=handler)
    result = _runner.invoke(app, ["run", "do a thing"])

    assert result.exit_code == 1
    assert "cannot be used as the Aithernet mission API" in _text(result)
    assert calls == []


# -- unit-level guarantees for the safe helpers --------------------------------------------------


def _resp(status, ctype, body, *, method="GET", path="/missions", server=None):
    headers = {"content-type": ctype}
    if server:
        headers["server"] = server
    return httpx.Response(status, headers=headers, text=body,
                          request=httpx.Request(method, f"http://127.0.0.1:8080{path}"))


def test_node_json_passes_through_valid_json():
    resp = _resp(200, "application/json", '{"ok": true}')
    assert cli._node_json(resp, subsystem="mission_api") == {"ok": True}


def test_node_json_surfaces_json_detail_on_error(capsys):
    resp = _resp(404, "application/json", '{"detail": "mission not found"}')
    with pytest.raises(typer.Exit) as exc:
        cli._node_json(resp, subsystem="mission_api")
    assert exc.value.exit_code == 1
    assert "mission not found" in capsys.readouterr().err


def test_node_json_html_is_foreign_endpoint_error(capsys):
    resp = _resp(200, "text/html", _NGINX_405, server="nginx/1.27.5")
    with pytest.raises(typer.Exit) as exc:
        cli._node_json(resp, subsystem="mission_api")
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "non-JSON response" in err
    assert "nginx/1.27.5" in err  # server banner is a safe hint
    assert "<html>" not in err


def test_strip_html_tags_removes_scripts_and_tags():
    stripped = cli._strip_html_tags(_SPA_HTML)
    assert "<" not in stripped and ">" not in stripped
    assert "sk-secret" not in stripped  # script bodies dropped
    assert "Aithernet Portal" in stripped


def test_safe_error_never_contains_authorization(capsys):
    # the request carried a bearer token; the structured error must NEVER echo request headers.
    resp = _resp(500, "text/html", "<html><body>Internal Server Error</body></html>")
    resp.request.headers["authorization"] = "Bearer sk-should-not-appear"
    resp.request.headers["cookie"] = "session=should-not-appear"
    with pytest.raises(typer.Exit):
        cli._node_json(resp, subsystem="mission_api")
    err = capsys.readouterr().err
    assert "sk-should-not-appear" not in err
    assert "should-not-appear" not in err
    assert "Authorization" not in err
