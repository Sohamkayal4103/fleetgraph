"""beta.5 Part B/C/F — the Aithernet-managed CatGPT Gateway sidecar.

Covers managed setup (token generated + stored via managed secrets, provider config uses
api_key_ref not a raw token, status never prints the token), the docker lifecycle through a mocked
runner, the gateway contract (JSON endpoints; non-JSON/HTML flagged), and the login-not-ready state.
"""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from aithernet.catgpt.contract import GatewayStatus, classify_gateway_status, verify_contract
from aithernet.catgpt.manager import (
    API_KEY_REF,
    CONTAINER_NAME,
    DEFAULT_IMAGE,
    CatGptGatewayManager,
    CatGptManagerError,
    RunResult,
)

TOKEN_MARKER = "tok-must-never-appear-1234567890"


class FakeRunner:
    """A mocked docker/process layer: records commands, returns canned results."""

    def __init__(self, *, available: bool = True, accessible: bool = True, running: bool = False,
                 fail: bool = False, image_present: bool = True) -> None:
        self._available = available
        self._accessible = accessible
        self._running = running
        self._fail = fail
        self._image_present = image_present
        self.calls: list[list[str]] = []
        self.loaded: list[str] = []

    def available(self) -> bool:
        return self._available

    def accessible(self) -> bool:
        return self._available and self._accessible

    def image_present(self, tag: str, *, timeout: float = 20.0) -> bool:
        return self._available and self._image_present

    def load_image_tar(self, tar, *, timeout: float = 900.0) -> RunResult:
        self.loaded.append(str(tar))
        self._image_present = True          # after a successful load the image is present
        return RunResult(0, f"Loaded image: {DEFAULT_IMAGE}", "")

    def run(self, args, *, timeout: float = 120.0) -> RunResult:
        self.calls.append(list(args))
        if self._fail:
            return RunResult(1, "", "boom")
        if args[:2] == ["image", "inspect"]:
            return RunResult(0 if self._image_present else 1, "", "")
        if "ps" in args:
            return RunResult(0, f"{CONTAINER_NAME}\n" if self._running else "", "")
        if "logs" in args:
            return RunResult(0, "gateway starting up\nlistening on :8000\n", "")
        return RunResult(0, "done", "")


def _mgr(tmp_path, **kw) -> CatGptGatewayManager:
    return CatGptGatewayManager(
        state_root=tmp_path / "state",
        secret_env_file=tmp_path / "secret.env",
        runner=kw.pop("runner", FakeRunner()),
    )


# -- F.4: managed setup --------------------------------------------------------------------------


def test_setup_generates_token_and_stores_via_managed_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv(API_KEY_REF, raising=False)
    mgr = _mgr(tmp_path)
    summary = mgr.setup(provider="chatgpt")

    # token stored in the managed secret file (NAME=value), and NOT the raw token in the summary
    secret_file = tmp_path / "secret.env"
    assert secret_file.is_file()
    body = secret_file.read_text()
    assert f"{API_KEY_REF}=" in body
    assert TOKEN_MARKER not in body  # sanity
    assert "token" not in json.dumps(summary).lower() or summary["token_stored"] is True
    assert "api_token" not in summary
    assert summary["api_key_ref"] == API_KEY_REF
    assert summary["loopback_only"] is True
    # the secret file is 0600
    mode = stat.S_IMODE(os.stat(secret_file).st_mode)
    assert mode == 0o600


def test_setup_writes_loopback_compose_and_0600_env(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.setup(provider="claude", api_port=8000, novnc_port=6080)

    compose = mgr.compose_path.read_text()
    assert '"127.0.0.1:8000:8000"' in compose      # API bound to loopback
    assert '"127.0.0.1:6080:6080"' in compose      # noVNC bound to loopback
    assert "0.0.0.0:8000:8000" not in compose      # never world-bound by default
    assert CONTAINER_NAME in compose

    env_mode = stat.S_IMODE(os.stat(mgr.env_path).st_mode)
    assert env_mode == 0o600
    env = mgr.env_path.read_text()
    assert "PROVIDER=claude" in env
    assert "API_TOKEN=" in env  # the container needs it; file is 0600 + user-owned


def test_setup_configures_coordinator_with_ref_not_token(tmp_path, monkeypatch):
    monkeypatch.delenv(API_KEY_REF, raising=False)
    mgr = _mgr(tmp_path)
    mgr.setup(provider="chatgpt", configure_coordinator=True)

    node_yaml = (tmp_path / "state" / "config" / "node.yaml").read_text()
    assert "provider: catgpt_gateway" in node_yaml
    assert f"api_key: env:{API_KEY_REF}" in node_yaml   # reference, never the value
    # the raw token value must not be written into node.yaml
    token = (tmp_path / "secret.env").read_text().split("=", 1)[1].strip()
    assert token and token not in node_yaml


def test_setup_rejects_bad_provider(tmp_path):
    mgr = _mgr(tmp_path)
    with pytest.raises(CatGptManagerError):
        mgr.setup(provider="gemini")


def test_setup_source_dir_requires_dockerfile(tmp_path):
    mgr = _mgr(tmp_path)
    missing = tmp_path / "nope"
    missing.mkdir()
    with pytest.raises(CatGptManagerError):
        mgr.setup(provider="chatgpt", source_dir=str(missing))


# -- lifecycle via the mocked docker layer -------------------------------------------------------


def test_start_stop_logs_use_compose_project_isolation(tmp_path):
    runner = FakeRunner(running=True)
    mgr = _mgr(tmp_path, runner=runner)
    mgr.setup(provider="chatgpt")

    assert mgr.start().ok
    assert mgr.stop().ok
    assert mgr.logs(tail=50).ok
    # every compose invocation is scoped to the Aithernet project (isolated from a hand-run catgpt)
    compose_calls = [c for c in runner.calls if c[:1] == ["compose"]]
    assert compose_calls
    for c in compose_calls:
        assert "-p" in c and "aithernet-catgpt" in c
    # start issued `up -d`
    assert any("up" in c and "-d" in c for c in compose_calls)
    # stop issued `down`
    assert any("down" in c for c in compose_calls)


def test_start_build_mode_adds_build_flag(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Dockerfile").write_text("FROM scratch\n")
    runner = FakeRunner()
    mgr = _mgr(tmp_path, runner=runner)
    mgr.setup(provider="chatgpt", source_dir=str(src))
    mgr.start()
    assert any("up" in c and "--build" in c for c in runner.calls)


# -- F.4: status never prints the token; container-state reporting -------------------------------


def test_status_reports_container_state_without_token(tmp_path):
    runner = FakeRunner(running=True)
    mgr = _mgr(tmp_path, runner=runner)
    mgr.setup(provider="chatgpt")
    data = mgr.status(probe=False)

    assert data["managed_gateway"] is True
    assert data["container_running"] is True
    assert data["credential_available"] is True
    assert data["api_key_ref"] == API_KEY_REF
    # the token value must never appear anywhere in the status payload
    token = (tmp_path / "secret.env").read_text().split("=", 1)[1].strip()
    assert token and token not in json.dumps(data)


# -- F.5: gateway contract -----------------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_contract_ok_for_wellbehaved_gateway():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        if p == "/ready":
            return httpx.Response(200, json={"ready": True})
        if p == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "catgpt-browser"}]})
        return httpx.Response(404, json={"error": {"type": "not_found", "status": 404}})

    rep = verify_contract("http://127.0.0.1:8000/v1", api_key="t", client=_client(handler))
    assert rep.endpoint_reachable and rep.ok
    assert rep.models == ["catgpt-browser"]
    assert classify_gateway_status(container_running=True, report=rep) == GatewayStatus.READY


def test_contract_flags_nginx_html_on_models():
    """A proxy returning nginx HTML for /v1/models is a contract violation, not a model list."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<html><h1>405 Not Allowed</h1><hr>nginx</html>")

    rep = verify_contract("http://127.0.0.1:8000/v1", api_key="t", client=_client(handler))
    assert rep.endpoint_reachable
    assert not rep.ok
    assert "JSON" in rep.detail or "json" in rep.detail
    assert classify_gateway_status(container_running=True, report=rep) == GatewayStatus.ERROR


def test_contract_stopped_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    rep = verify_contract("http://127.0.0.1:8000/v1", api_key="t", client=_client(handler))
    assert not rep.endpoint_reachable
    assert classify_gateway_status(container_running=False, report=rep) == GatewayStatus.STOPPED
    assert classify_gateway_status(container_running=True, report=rep) == GatewayStatus.STARTING


# -- F.6: login-not-ready ------------------------------------------------------------------------


def test_login_required_when_models_unauthorized():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(401, json={"error": {"type": "login_required",
                                                   "message": "browser session not logged in"}})

    rep = verify_contract("http://127.0.0.1:8000/v1", api_key="t", client=_client(handler))
    assert rep.endpoint_reachable
    assert rep.detail == GatewayStatus.LOGIN_REQUIRED
    assert (classify_gateway_status(container_running=True, report=rep)
            == GatewayStatus.LOGIN_REQUIRED)


def test_login_required_when_models_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": []})

    rep = verify_contract("http://127.0.0.1:8000/v1", api_key="t", client=_client(handler))
    assert rep.endpoint_reachable
    assert rep.models == []
    assert (classify_gateway_status(container_running=True, report=rep)
            == GatewayStatus.LOGIN_REQUIRED)


def test_status_login_required_via_manager_probe(tmp_path):
    """End-to-end: manager.status() with an injected mock client surfaces login_required."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": []})

    runner = FakeRunner(running=True)
    mgr = _mgr(tmp_path, runner=runner)
    mgr.setup(provider="chatgpt")
    data = mgr.status(probe=True, client=_client(handler))
    assert data["gateway_status"] == GatewayStatus.LOGIN_REQUIRED
    assert data["endpoint_reachable"] is True
    assert data["model"] is None


# -- CLI surface (Part B/D) ----------------------------------------------------------------------


def _cli_text(result) -> str:
    parts = [result.output or ""]
    try:
        if result.stderr:
            parts.append(result.stderr)
    except ValueError:
        pass
    return "".join(parts)


def test_cli_catgpt_setup_and_status(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from aithernet import cli

    runner = FakeRunner(running=True)
    mgr = CatGptGatewayManager(
        state_root=tmp_path / "state", secret_env_file=tmp_path / "secret.env", runner=runner)
    monkeypatch.setattr(cli, "_catgpt_manager", lambda: mgr)
    cli_runner = CliRunner()

    res = cli_runner.invoke(cli.app, ["catgpt", "setup", "--provider", "chatgpt"])
    out = _cli_text(res)
    assert res.exit_code == 0, out
    assert "http://127.0.0.1:8000/v1" in out
    assert "loopback-only" in out
    assert "noVNC" in out
    # the token is never printed; only the reference name
    token = (tmp_path / "secret.env").read_text().split("=", 1)[1].strip()
    assert token and token not in out
    assert "CATGPT_GATEWAY_API_KEY" in out

    res2 = cli_runner.invoke(cli.app, ["catgpt", "status", "--no-probe", "--json"])
    assert "\"managed_gateway\": true" in _cli_text(res2).lower() or \
        "managed_gateway" in _cli_text(res2)


def test_cli_catgpt_start_stop_logs(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from aithernet import cli

    runner = FakeRunner(running=True)
    mgr = CatGptGatewayManager(
        state_root=tmp_path / "state", secret_env_file=tmp_path / "secret.env", runner=runner)
    monkeypatch.setattr(cli, "_catgpt_manager", lambda: mgr)
    cli_runner = CliRunner()
    cli_runner.invoke(cli.app, ["catgpt", "setup"])

    assert cli_runner.invoke(cli.app, ["catgpt", "start"]).exit_code == 0
    assert cli_runner.invoke(cli.app, ["catgpt", "logs", "--tail", "10"]).exit_code == 0
    assert cli_runner.invoke(cli.app, ["catgpt", "stop"]).exit_code == 0
    assert any("up" in c and "-d" in c for c in runner.calls)
    assert any("down" in c for c in runner.calls)


# -- Part D: provider-status merges managed_gateway/gateway_status --------------------------------


def test_provider_status_merges_managed_gateway(monkeypatch, tmp_path):
    """providers.status() surfaces managed_gateway + gateway_status for a managed coordinator."""
    from aithernet.agents import providers as prov

    # a coordinator configured as catgpt_gateway in an isolated node.yaml
    prov.configure("coordinator", "catgpt_gateway",
                   base_url="http://127.0.0.1:8000/v1", api_key_ref=API_KEY_REF,
                   state_root=tmp_path)
    monkeypatch.setattr(prov, "_catgpt_managed_status",
                        lambda state_root=None: (True, "login_required", None))
    st = prov.status("coordinator", state_root=tmp_path)
    assert st["provider"] == "catgpt_gateway"
    assert st["managed_gateway"] is True
    assert st["gateway_status"] == "login_required"
    assert st["readiness"]["gateway_status"] == "login_required"


def test_provider_status_external_is_not_managed(monkeypatch, tmp_path):
    from aithernet.agents import providers as prov

    prov.configure("coordinator", "catgpt_gateway",
                   base_url="http://127.0.0.1:8000/v1", api_key_ref=API_KEY_REF,
                   state_root=tmp_path)
    monkeypatch.setattr(prov, "_catgpt_managed_status",
                        lambda state_root=None: (False, "not_managed", None))
    st = prov.status("coordinator", state_root=tmp_path)
    assert st["managed_gateway"] is False
    assert "gateway_status" not in st


# -- foreground `aithernet start` loads the managed secret (no mid-mission 401) -------------------


def test_foreground_start_loads_managed_secret(tmp_path, monkeypatch):
    """`aithernet start` (foreground) must load the 0600 managed secret store into the process env,
    like the systemd EnvironmentFile — otherwise the in-mission coordinator would 401."""
    import typer
    from typer.testing import CliRunner

    from aithernet import cli
    from aithernet.agents import providers as prov

    secret = tmp_path / "aithernet.env"
    secret.write_text("CATGPT_GATEWAY_API_KEY=probe-token-xyz\n")
    monkeypatch.setattr(prov, "secret_env_file", lambda: secret)
    monkeypatch.delenv("CATGPT_GATEWAY_API_KEY", raising=False)

    # stop start() right after it loads secrets (before it needs a real config/uvicorn)
    def _stop(*_a, **_k):
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "load_config", _stop)
    try:
        CliRunner().invoke(cli.app, ["start", "--config", str(tmp_path / "node.yaml")])
        assert os.environ.get("CATGPT_GATEWAY_API_KEY") == "probe-token-xyz"
    finally:
        # `aithernet start` loads the secret into the real os.environ (not via monkeypatch);
        # pop it so it never leaks into other tests in the same process.
        os.environ.pop("CATGPT_GATEWAY_API_KEY", None)


# -- Option A: shipped image runtime (no clone / build / pull / docker login) ---------------------


def _only_search_dir(mgr, d):
    """Constrain image-tar search to a single directory (hermetic; no cwd/opt fallbacks)."""
    from pathlib import Path
    mgr._image_search_dirs = lambda: [Path(d)]  # type: ignore[method-assign]


def test_start_loads_shipped_image_from_bundle(tmp_path):
    """Fresh client: Docker installed, image NOT present, a shipped tar in the bundle dir ->
    `start` loads the image locally (no Docker Hub pull) and comes up."""
    from aithernet.catgpt.manager import IMAGE_ARTIFACT

    runner = FakeRunner(running=True, image_present=False)
    mgr = _mgr(tmp_path, runner=runner)
    mgr.setup(provider="chatgpt")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / IMAGE_ARTIFACT).write_bytes(b"fake-image-tar")
    _only_search_dir(mgr, bundle)

    res = mgr.start()
    assert res.ok
    assert runner.loaded == [str(bundle / IMAGE_ARTIFACT)], "must load the shipped tar exactly once"
    # NEVER a registry pull; compose relies on the locally-loaded image + pull_policy: never
    assert "pull_policy: never" in mgr.compose_path.read_text()
    assert not any("pull" in c for c in runner.calls)
    assert DEFAULT_IMAGE in mgr.compose_path.read_text()


def test_start_missing_image_tar_is_clear_aithernet_error_not_docker_pull(tmp_path):
    """No shipped tar anywhere -> a clear Aithernet error, never Docker's pull-access-denied."""
    runner = FakeRunner(image_present=False)
    mgr = _mgr(tmp_path, runner=runner)
    mgr.setup(provider="chatgpt")
    _only_search_dir(mgr, tmp_path / "empty")  # nonexistent/empty -> no tar

    with pytest.raises(CatGptManagerError) as exc:
        mgr.start()
    msg = str(exc.value)
    assert "Managed CatGPT Gateway image is missing from the release bundle" in msg
    assert "aithernet components install catgpt-gateway" in msg
    assert "pull access denied" not in msg.lower()
    assert not runner.loaded


def test_ensure_image_local_present_is_noop(tmp_path):
    runner = FakeRunner(image_present=True)
    mgr = _mgr(tmp_path, runner=runner)
    mgr.setup(provider="chatgpt")
    info = mgr.ensure_image()
    assert info["source"] == "local" and info["present"] is True
    assert not runner.loaded  # already present -> nothing loaded, nothing pulled


def test_setup_reports_bundled_image_source_mode(tmp_path):
    from aithernet.catgpt.manager import IMAGE_ARTIFACT

    mgr = _mgr(tmp_path, runner=FakeRunner(image_present=True))
    summary = mgr.setup(provider="chatgpt")
    assert summary["source_mode"] == "bundled image"
    assert summary["image"] == DEFAULT_IMAGE
    assert summary["image_artifact"] == IMAGE_ARTIFACT
    assert summary["image_loaded"] is True
    assert summary["docker_installed"] is True


def test_status_exposes_docker_and_image_fields(tmp_path):
    mgr = _mgr(tmp_path, runner=FakeRunner(running=True, image_present=True))
    mgr.setup(provider="chatgpt")
    data = mgr.status(probe=False)
    for key in ("docker_installed", "docker_accessible", "managed_image_present",
                "managed_image_source", "container_running", "endpoint_reachable",
                "login_required", "ready"):
        assert key in data, f"status missing {key}"
    assert data["docker_installed"] is True
    assert data["managed_image_present"] is True
    assert data["managed_image_source"] == "local_cache"


def test_docker_missing_status_reports_and_no_pull(tmp_path):
    mgr = _mgr(tmp_path, runner=FakeRunner(available=False, accessible=False, image_present=False))
    mgr.setup(provider="chatgpt")
    data = mgr.status(probe=False)
    assert data["docker_installed"] is False
    assert data["docker_accessible"] is False
    assert data["managed_image_present"] is False


def test_docker_install_plan_is_confirmed_sudo_only(tmp_path):
    mgr = _mgr(tmp_path)
    plan = mgr.docker_install_plan()
    # Ubuntu install, no docker login, no git clone; every step is explicit sudo argv (no shell).
    joined = [" ".join(c) for c in plan]
    assert any("apt install" in j and "docker.io" in j for j in joined)
    assert any("usermod -aG docker" in j for j in joined)
    assert all(c[0] == "sudo" for c in plan)
    assert not any("login" in j for j in joined)         # never docker login
    assert not any("git" in j for j in joined)           # never git clone


def test_no_secret_in_compose_or_config(tmp_path, monkeypatch):
    """The bearer token/VNC password live only in the 0600 gateway.env — never in the image
    reference, compose, or config.json (so nothing secret ships in the image or logs)."""
    monkeypatch.delenv(API_KEY_REF, raising=False)
    mgr = _mgr(tmp_path)
    mgr.setup(provider="chatgpt")
    token = (tmp_path / "secret.env").read_text().split("=", 1)[1].strip()
    compose = mgr.compose_path.read_text()
    config = mgr.config_path.read_text()
    assert token and token not in compose and token not in config
    # compose wires secrets via env_file (not baked ENV), so the image never contains them
    assert "env_file:" in compose
    logs = mgr.logs()
    assert token not in (logs.stdout + logs.stderr)


def test_cli_install_runtime_docker_missing_is_actionable(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from aithernet import cli

    mgr = CatGptGatewayManager(
        state_root=tmp_path / "state", secret_env_file=tmp_path / "secret.env",
        runner=FakeRunner(available=False, accessible=False))
    monkeypatch.setattr(cli, "_catgpt_manager", lambda: mgr)
    res = CliRunner().invoke(cli.app, ["catgpt", "install-runtime"])  # non-interactive, no --yes
    out = _cli_text(res)
    assert res.exit_code == 1
    assert "NOT installed" in out or "not installed" in out.lower()
    assert "apt install" in out and "docker.io" in out
    assert "--install-docker" in out or "install Docker yourself" in out


def test_cli_components_install_catgpt_gateway_loads_image(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from aithernet import cli
    from aithernet.catgpt.manager import IMAGE_ARTIFACT

    runner = FakeRunner(image_present=False)
    mgr = CatGptGatewayManager(
        state_root=tmp_path / "state", secret_env_file=tmp_path / "secret.env", runner=runner)
    mgr.setup(provider="chatgpt")
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / IMAGE_ARTIFACT).write_bytes(b"tar")
    _only_search_dir(mgr, bundle)
    monkeypatch.setattr(cli, "_catgpt_manager", lambda: mgr)
    res = CliRunner().invoke(cli.app, ["components", "install", "catgpt-gateway"])
    out = _cli_text(res)
    assert res.exit_code == 0, out
    assert "catgpt-gateway image" in out
    assert runner.loaded
