"""beta.4 parser tests for the new `aithernet agents` provider commands (no live providers)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from aithernet.cli import app as cli_app

runner = CliRunner()


def _run(args, env_root):
    return runner.invoke(cli_app, args, env={"AITHERNET_STATE_ROOT": str(env_root)})


def test_list_providers_shows_capabilities_and_billing(tmp_path):
    r = _run(["agents", "list-providers", "coordinator", "--json"], tmp_path)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    keys = {p["key"] for p in data["coordinator"]}
    assert {"claude_cli", "anthropic_bedrock", "openai_api", "gemini_vertex"} <= keys
    claude = next(p for p in data["coordinator"] if p["key"] == "claude_cli")
    assert claude["billing"] == "subscription allowance"
    assert "reasoning" in claude["capabilities"] and claude["eligible"] is True


def test_inspect_provider(tmp_path):
    r = _run(["agents", "inspect-provider", "codex_cli"], tmp_path)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["role"] == "coding" and data["pool_family"] == "chatgpt_codex_subscription"
    assert "coding" in data["capabilities"]


def test_presets_listed(tmp_path):
    r = _run(["agents", "presets", "--json"], tmp_path)
    assert r.exit_code == 0, r.output
    names = {p["name"] for p in json.loads(r.output)}
    assert "single_provider_claude" in names and "provider_diverse" in names


def test_capacity_reports_pools(tmp_path):
    r = _run(["agents", "capacity"], tmp_path)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert "pools" in data and "shared_pool" in data


def test_set_and_show_fallbacks(tmp_path):
    set_r = _run(["agents", "set-fallbacks", "coordinator", "anthropic,openai_api"], tmp_path)
    assert set_r.exit_code == 0, set_r.output
    show_r = _run(["agents", "fallbacks", "coordinator"], tmp_path)
    assert show_r.exit_code == 0, show_r.output
    chain = json.loads(show_r.output)["chain"]
    assert "anthropic" in chain and "openai_api" in chain


def test_set_fallbacks_rejects_role_incapable(tmp_path):
    # codex_cli is coding-only; it must be rejected from a coordinator fallback chain.
    r = _run(["agents", "set-fallbacks", "coordinator", "codex_cli"], tmp_path)
    assert r.exit_code != 0


def test_disconnect_sets_disabled(tmp_path):
    r = _run(["agents", "disconnect", "coding"], tmp_path)
    assert r.exit_code == 0, r.output
