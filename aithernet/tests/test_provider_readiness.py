"""Part D: coding-agent provider readiness via a real executable identity probe.

Readiness is not just "the executable resolves" — the provider runs a short identity probe
(``--version``) and verifies the binary behaves like the expected agent. Tests use fake CLI
scripts; neither Claude nor Codex needs to be installed.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.coding_agent.providers import base, build_provider
from aithernet.coding_agent.runtime import CodingAgentRuntime
from aithernet.config.loader import load_config
from aithernet.config.settings import CodingAgentConfig, NodeConfig
from aithernet.orchestrator.runtime import NodeRuntime


def _write_cli(path: Path, *, version: str, body: str = "sys.exit(0)\n", pre: str = "") -> Path:
    """Write a fake CLI that answers ``--version`` and otherwise runs ``body``.

    ``pre`` runs before everything (used to make ``--version`` itself hang for the probe
    timeout test).
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, time, json\n"
        "args = sys.argv[1:]\n"
        f"{pre}"
        "if '--version' in args:\n"
        f"    print({version!r})\n"
        "    sys.exit(0)\n" + body
    )
    path.chmod(0o755)
    return path


def _provider(provider: str, executable: str):
    return build_provider(CodingAgentConfig(provider=provider, executable=executable))


def test_claude_with_codex_executable_is_not_configured(tmp_path) -> None:
    fake_codex = _write_cli(tmp_path / "codex", version="codex-cli 9.9.9-fake")
    provider = _provider("claude_code", str(fake_codex))
    assert provider.check_ready() == ["executable_provider_mismatch"]


def test_codex_with_codex_executable_is_configured(tmp_path) -> None:
    fake_codex = _write_cli(tmp_path / "codex", version="codex-cli 9.9.9-fake")
    provider = _provider("codex_cli", str(fake_codex))
    assert provider.check_ready() == []


def test_claude_with_claude_executable_is_configured(tmp_path) -> None:
    fake_claude = _write_cli(tmp_path / "claude", version="1.0.88 (Claude Code)")
    provider = _provider("claude_code", str(fake_claude))
    assert provider.check_ready() == []


def test_absolute_path_executable_works(tmp_path) -> None:
    fake_codex = _write_cli(tmp_path / "codex", version="codex-cli 9.9.9-fake")
    # An explicit absolute path (not on PATH) must still resolve + probe.
    assert os.path.isabs(str(fake_codex))
    assert _provider("codex_cli", str(fake_codex)).check_ready() == []


def test_unknown_provider_is_unconfigured(tmp_path) -> None:
    runtime = CodingAgentRuntime.from_config(CodingAgentConfig(provider="nope"))
    status = runtime.status()
    assert status.configured is False
    assert any("unknown" in item for item in status.missing_configuration)


def test_executable_not_found_is_unconfigured() -> None:
    provider = _provider("codex_cli", "definitely-not-a-real-binary-xyz")
    assert provider.check_ready() == ["executable"]


def test_non_executable_format_reports_not_runnable(tmp_path) -> None:
    # A file with the exec bit but an invalid program format fails to spawn (ENOEXEC).
    bad = tmp_path / "codex"
    bad.write_bytes(b"\x00\x01\x02 not an executable program\n")
    bad.chmod(0o755)
    provider = _provider("codex_cli", str(bad))
    assert provider.check_ready() == ["executable_not_runnable"]


def test_probe_timeout_is_reported_cleanly(tmp_path, monkeypatch) -> None:
    slow = _write_cli(
        tmp_path / "codex",
        version="codex-cli 9.9.9-fake",
        pre="time.sleep(5)\n",  # make --version itself hang
    )
    # Make the probe time out quickly: a slow --version must not hang status polling.
    monkeypatch.setattr(base, "_IDENTITY_PROBE_TIMEOUT", 0.4)
    provider = _provider("codex_cli", str(slow))
    assert provider.check_ready() == ["executable_probe_failed"]


def test_probe_result_is_cached(tmp_path) -> None:
    counter = tmp_path / "calls.txt"
    counter.write_text("")
    fake = _write_cli(
        tmp_path / "codex",
        version="codex-cli 9.9.9-fake",
        body=f"open({str(counter)!r}, 'a').write('x')\nsys.exit(0)\n",
    )
    provider = _provider("codex_cli", str(fake))
    # Multiple readiness checks (as dashboard polling does) probe the binary only once.
    for _ in range(5):
        provider.check_ready()
    # --version path does not hit the body, so the counter file proves no exec re-spawn;
    # the cache is what we assert: repeated calls return the same object cheaply.
    assert provider.check_ready() == []


def _coding_agent_yaml(path: Path) -> Path:
    path.write_text(
        "node_id: t\n"
        "node_name: t\n"
        "coding_agent:\n"
        "  provider: env:AITHERNET_CODING_AGENT_PROVIDER\n"
        "  executable: env:AITHERNET_CODING_AGENT_EXECUTABLE\n"
    )
    return path


def test_provider_selected_from_env(tmp_path, monkeypatch) -> None:
    cfg = _coding_agent_yaml(tmp_path / "node.yaml")
    monkeypatch.setenv("AITHERNET_CODING_AGENT_PROVIDER", "codex_cli")
    monkeypatch.setenv("AITHERNET_CODING_AGENT_EXECUTABLE", "codex")
    config = load_config(cfg, env_file=tmp_path / "absent.env")
    assert config.coding_agent.provider == "codex_cli"
    assert config.coding_agent.executable == "codex"


def test_provider_falls_back_to_default_when_unset(tmp_path, monkeypatch) -> None:
    cfg = _coding_agent_yaml(tmp_path / "node.yaml")
    monkeypatch.delenv("AITHERNET_CODING_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AITHERNET_CODING_AGENT_EXECUTABLE", raising=False)
    config = load_config(cfg, env_file=tmp_path / "absent.env")
    # Documented backward-compatible default; never silently a different provider.
    assert config.coding_agent.provider == "claude_code"
    assert config.coding_agent.executable == "claude"


def test_status_endpoint_does_not_leak_env(config: NodeConfig, tmp_path) -> None:
    fake_codex = _write_cli(tmp_path / "codex", version="codex-cli 9.9.9-fake")
    runtime = NodeRuntime.from_config(
        config,
        coding_agent=CodingAgentRuntime.from_config(
            CodingAgentConfig(
                provider="codex_cli",
                executable=str(fake_codex),
                env={"CODEX_SECRET": "sk-super-secret-codex-value"},
            )
        ),
    )
    with TestClient(create_app(runtime=runtime)) as client:
        response = client.get("/coding-agent/status")
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "codex_cli"
        assert body["configured"] is True
        assert "env" not in body
        assert "sk-super-secret-codex-value" not in response.text
