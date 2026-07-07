"""beta.9 Defect 7: `aithernet agents connect coordinator|coding` is a guided, provider-agnostic
flow — list options, configure, persist the runtime PATH, restart + verify — without the customer
knowing adapter ids or editing env files."""

from __future__ import annotations

from typer.testing import CliRunner

from aithernet.cli import app as cli_app


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "state" / "config").mkdir(parents=True, exist_ok=True)


def test_connect_lists_options_when_no_provider(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    # No TTY in the test runner -> it lists options and asks for --provider (exit 2), never hangs.
    result = CliRunner().invoke(cli_app, ["agents", "connect", "coordinator"])
    assert result.exit_code == 2, result.output
    assert "Supported coordinator options" in result.output
    assert "Gemini API" in result.output
    assert "--provider" in result.output


def test_connect_api_provider_requires_key_reference(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli_app, ["agents", "connect", "coordinator", "--provider", "gemini_api", "--no-restart"])
    assert result.exit_code == 2, result.output
    assert "set-secret" in result.output  # guided to store the key, never asked for the value


def test_connect_configures_and_defers_verification(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    # codex_cli is a CLI provider: no key needed; with no real codex installed, verification defers
    # (configured cleanly, one actionable next step) — and never restarts in the test.
    result = CliRunner().invoke(
        cli_app, ["agents", "connect", "coding", "--provider", "codex_cli", "--no-restart"])
    assert result.exit_code == 0, result.output
    assert "Configured coding: Codex" in result.output  # display: "Codex — signed in through ChatGPT"
    # verification either succeeds (real codex present) or defers with one actionable next step
    assert ("Verified" in result.output) or ("agents test coding --live" in result.output)
    # the selection was written to the canonical node.yaml
    import yaml
    cfg = yaml.safe_load((tmp_path / "state" / "config" / "node.yaml").read_text())
    assert cfg["coding_agent"]["provider"] == "codex_cli"


def test_connect_rejects_unknown_role(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli_app, ["agents", "connect", "planner"])
    assert result.exit_code == 2
    assert "coordinator" in result.output and "coding" in result.output
