"""Tests for customer-facing AI provider configuration (Stage 14G, PHASE 6 + corrective).

The single source of truth is the canonical NodeConfig YAML; the selected provider must be the
provider the runtime actually builds. Covers BYO catalogue honesty, executable detection,
configure/remove writing through NodeConfig, sanitised status, the bounded readiness test against
the runtime config, secret-safe env-ref handling, runtime identity, and the one-time agents.json
migration. No real LLM call, no network, no credentials.
"""

from __future__ import annotations

import json

import pytest

from aithernet.agents import providers as prov


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("AITHERNET_CONFIG", raising=False)  # -> <state_root>/config/node.yaml
    return tmp_path


# --- catalogue (BYO + truthful classification) -----------------------------
def test_preferred_beta_defaults_are_gemini_and_codex():
    coord_pref = {p.key for p in prov.providers_for(prov.COORDINATOR) if p.preferred_beta}
    coding_pref = {p.key for p in prov.providers_for(prov.CODING) if p.preferred_beta}
    assert coord_pref == {"gemini_cli"}
    assert coding_pref == {"codex_cli"}


def test_byo_providers_preserved_and_truthfully_classified():
    coding = {p.key for p in prov.providers_for(prov.CODING)}
    assert {"codex_cli", "claude_code", "disabled"} <= coding
    coord = {p.key for p in prov.providers_for(prov.COORDINATOR)}
    assert {"gemini_cli", "anthropic", "openai_compatible", "openai_compatible_local"} <= coord
    assert prov.get_info(prov.CODING, "claude_code").support_level == prov.AUTO_TESTED
    for p in prov.CATALOGUE:
        assert p.support_level in prov.SUPPORT_LEVELS


def test_no_provider_claims_real_tested_before_operator_gate():
    assert all(p.support_level != prov.REAL_TESTED for p in prov.CATALOGUE)


# --- canonical source of truth: selection == execution ---------------------
def test_configure_writes_canonical_nodeconfig(tmp_path):
    prov.configure(prov.COORDINATOR, "gemini_cli", executable="gemini")
    cfg_path = prov.node_config_path()
    assert cfg_path == tmp_path / "config" / "node.yaml"
    assert oct(cfg_path.stat().st_mode)[-3:] == "600"
    # the canonical loader reads it and the runtime would build the SAME provider
    from aithernet.config import load_config
    nc = load_config(cfg_path)
    assert nc.coordinator.provider == "gemini_cli"
    from aithernet.coordinator.providers import build_provider
    built = build_provider(nc.coordinator)
    assert built is not None and built.name == "gemini_cli"   # selected == executed


def test_coding_selection_is_executed(tmp_path):
    prov.configure(prov.CODING, "codex_cli", executable="codex")
    from aithernet.coding_agent.providers import build_provider
    from aithernet.config import load_config
    nc = load_config(prov.node_config_path())
    assert nc.coding_agent.provider == "codex_cli"
    assert build_provider(nc.coding_agent).name == "codex_cli"


def test_only_one_source_of_truth_no_agents_json_written(tmp_path):
    prov.configure(prov.COORDINATOR, "gemini_cli")
    # configuration lives in node.yaml only; no agents.json selection store is created
    assert (tmp_path / "config" / "node.yaml").is_file()
    assert not (tmp_path / "config" / "agents.json").is_file()


# --- secrets: env-ref only, never a value ----------------------------------
def test_api_key_stored_as_env_reference_not_value(tmp_path):
    prov.configure(prov.COORDINATOR, "anthropic", api_key_ref="ANTHROPIC_API_KEY")
    raw = (tmp_path / "config" / "node.yaml").read_text()
    assert "env:ANTHROPIC_API_KEY" in raw
    assert "sk-" not in raw
    st = prov.status(prov.COORDINATOR)
    assert st["api_key_ref"] == "ANTHROPIC_API_KEY"
    assert st["api_key_present"] is False           # env var unset -> not present, never the value


def test_configure_rejects_key_as_ref():
    with pytest.raises(ValueError):
        prov.configure(prov.COORDINATOR, "anthropic", api_key_ref="sk-abc123definitelyakey")


def test_configure_unknown_provider_rejected():
    with pytest.raises(ValueError):
        prov.configure(prov.COORDINATOR, "not-a-provider")


# --- detection -------------------------------------------------------------
def test_detect_executable_missing(monkeypatch):
    monkeypatch.setattr(prov.shutil, "which", lambda e: None)
    p = prov.detect_executable("gemini")
    assert p.found is False and p.version is None


def test_detect_executable_found(monkeypatch):
    monkeypatch.setattr(prov.shutil, "which", lambda e: "/usr/bin/" + e)

    class _P:
        stdout, stderr, returncode = "gemini 1.2.3\n", "", 0

    monkeypatch.setattr(prov.subprocess, "run", lambda *a, **k: _P())
    p = prov.detect_executable("gemini")
    assert p.found and p.version == "1.2.3"


# --- status / remove -------------------------------------------------------
def test_status_reads_canonical_config(monkeypatch):
    prov.configure(prov.COORDINATOR, "gemini_cli", executable="gemini")
    monkeypatch.setattr(prov, "detect_executable",
                        lambda e: prov.Probe("/usr/bin/gemini", True, "0.46.0", "ok"))
    st = prov.status(prov.COORDINATOR)
    assert st["configured"] and st["provider"] == "gemini_cli"
    assert st["executable_version"] == "0.46.0"
    assert st["auth_readiness"] == "executable-present-auth-unverified"
    assert st["runtime_identity"]["identity_model"] == "workstation"
    assert st["config_source"].endswith("config/node.yaml")


def test_remove_resets_to_disabled():
    prov.configure(prov.CODING, "codex_cli")
    out = prov.remove(prov.CODING)
    assert out["provider"] == "disabled"
    assert prov.status(prov.CODING)["configured"] is False


# --- bounded readiness test against the runtime config ---------------------
class _FakeProvider:
    name = "fake"

    def __init__(self, missing):
        self._missing = missing

    def check_ready(self):
        return list(self._missing)

    async def healthcheck(self):
        return {"model": "fake", "ok": True, "latency_ms": 12, "secret": "should-be-dropped"}


def test_test_disabled_is_ready():
    prov.configure(prov.COORDINATOR, "disabled")
    r = prov.test(prov.COORDINATOR)
    assert r["ready"] and r["status"] == "disabled"


def test_test_coordinator_ready_without_live(monkeypatch):
    prov.configure(prov.COORDINATOR, "gemini_cli", executable="gemini")
    import aithernet.coordinator.providers as cp
    monkeypatch.setattr(cp, "build_provider", lambda cfg: _FakeProvider([]))
    r = prov.test(prov.COORDINATOR)
    assert r["ready"] and r["status"] == "ready"
    # the outcome is persisted to the readiness cache; status reflects it
    assert prov.status(prov.COORDINATOR)["auth_readiness"] == "ready"


def test_test_coordinator_not_ready(monkeypatch):
    prov.configure(prov.COORDINATOR, "gemini_cli", executable="gemini")
    import aithernet.coordinator.providers as cp
    monkeypatch.setattr(cp, "build_provider", lambda cfg: _FakeProvider(["executable_not_found"]))
    r = prov.test(prov.COORDINATOR)
    assert r["ready"] is False and r["status"] == "not_ready"
    assert "executable_not_found" in r["missing"]


def test_test_live_sanitises_probe(monkeypatch):
    prov.configure(prov.COORDINATOR, "gemini_cli", executable="gemini")
    import aithernet.coordinator.providers as cp
    monkeypatch.setattr(cp, "build_provider", lambda cfg: _FakeProvider([]))
    r = prov.test(prov.COORDINATOR, live=True)
    assert r["ready"] and r["status"] == "ready"
    assert "secret" not in r["probe"]
    assert r["probe"]["model"] == "fake"


# --- live coding readiness: a REAL bounded invocation, not an executable probe ----
class _FakeCoding:
    """A coding adapter stand-in. ``echo`` controls whether execute() echoes the prompt (which
    carries the unique nonce), so we can simulate validated / non-validated / failing runs."""
    name = "codex_cli"

    def __init__(self, *, missing=None, echo=True, status="completed", raise_exc=False):
        self._missing = missing or []
        self._echo, self._status, self._raise = echo, status, raise_exc
        self.executed = False

    def check_ready(self):
        return list(self._missing)

    def identity_version(self):
        return "codex-cli 9.9.9"

    def _resolve_executable(self):
        return "/usr/bin/codex"

    async def execute(self, task):
        self.executed = True
        if self._raise:
            raise RuntimeError("boom")
        from aithernet.coding_agent.contracts import CodingTaskExecutionResult
        # echoing the objective returns the nonce embedded by the probe
        return CodingTaskExecutionResult(
            status=self._status, summary=(task.objective if self._echo else "no nonce here"))


def test_coding_non_live_is_executable_probe_only(monkeypatch):
    prov.configure(prov.CODING, "codex_cli", executable="codex")
    import aithernet.coding_agent.providers as cp
    fake = _FakeCoding()
    monkeypatch.setattr(cp, "build_provider", lambda cfg: fake)
    r = prov.test(prov.CODING)            # no --live
    assert r["ready"] and r["status"] == "ready" and r["live"] is False
    assert "executable" in r["detail"]
    assert fake.executed is False         # the adapter was NOT invoked


def test_coding_live_requires_real_invocation(monkeypatch, tmp_path):
    prov.configure(prov.CODING, "codex_cli", executable="codex")
    import aithernet.coding_agent.providers as cp
    fake = _FakeCoding(echo=True)
    monkeypatch.setattr(cp, "build_provider", lambda cfg: fake)
    r = prov.test(prov.CODING, live=True)
    assert fake.executed is True          # a real provider request occurred
    assert r["live"] is True and r["real_request"] is True
    assert r["ready"] is True and r["status"] == "ready"
    assert "live bounded Codex invocation succeeded" in r["detail"]
    assert r["nonce_validated"] is True and r["response_status"] == "completed"
    assert r["executed_provider"] == "codex_cli"      # selected == executed
    assert r["client_version"] == "codex-cli 9.9.9"
    assert r["workspace_cleaned"] is True             # temp workspace removed
    assert "~" in r["workspace"] or "/tmp" in r["workspace"]   # path shown safely


def test_coding_live_fails_without_validated_response(monkeypatch):
    prov.configure(prov.CODING, "codex_cli", executable="codex")
    import aithernet.coding_agent.providers as cp
    monkeypatch.setattr(cp, "build_provider", lambda cfg: _FakeCoding(echo=False))
    r = prov.test(prov.CODING, live=True)
    assert r["live"] is True and r["ready"] is False and r["status"] == "not_ready"
    assert r["nonce_validated"] is False


def test_coding_live_provider_failure_returns_not_ready(monkeypatch):
    prov.configure(prov.CODING, "codex_cli", executable="codex")
    import aithernet.coding_agent.providers as cp
    monkeypatch.setattr(cp, "build_provider", lambda cfg: _FakeCoding(raise_exc=True))
    r = prov.test(prov.CODING, live=True)
    assert r["live"] is True and r["ready"] is False and r["status"] == "error"
    assert r["workspace_cleaned"] is True             # workspace cleaned even on failure


def test_coding_live_failed_exit_is_not_ready(monkeypatch):
    prov.configure(prov.CODING, "codex_cli", executable="codex")
    import aithernet.coding_agent.providers as cp
    monkeypatch.setattr(cp, "build_provider",
                        lambda cfg: _FakeCoding(echo=True, status="failed"))
    r = prov.test(prov.CODING, live=True)
    assert r["ready"] is False             # nonce echoed but the run did not complete cleanly


def test_coding_live_uses_isolated_temp_workspace_not_repo(monkeypatch):
    prov.configure(prov.CODING, "codex_cli", executable="codex")
    seen = {}
    import aithernet.coding_agent.providers as cp

    class _Capture(_FakeCoding):
        async def execute(self, task):
            seen["workspace"] = task.workspace
            return await super().execute(task)

    monkeypatch.setattr(cp, "build_provider", lambda cfg: _Capture(echo=True))
    r = prov.test(prov.CODING, live=True)
    assert r["ready"] is True
    # the workspace is a fresh temp dir OUTSIDE the repo, and is removed afterwards
    import tempfile
    assert seen["workspace"].startswith(tempfile.gettempdir())
    assert "aithernet" in __import__("os").getcwd()   # repo cwd unaffected
    assert not __import__("os").path.exists(seen["workspace"])


def test_appliance_identity_recorded():
    prov.configure(prov.COORDINATOR, "anthropic", api_key_ref="ANTHROPIC_API_KEY",
                   identity_model="appliance")
    st = prov.status(prov.COORDINATOR)
    assert st["runtime_identity"]["identity_model"] == "appliance"
    assert st["api_key_ref"] == "ANTHROPIC_API_KEY"
    assert st["auth_readiness"] == "key-reference-set"


# --- one-time agents.json migration ----------------------------------------
def test_migrate_agents_json_folds_into_nodeconfig(tmp_path):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir(parents=True)
    (cfgdir / "agents.json").write_text(json.dumps({
        "coordinator": {"provider": "gemini_cli", "executable": "gemini"},
        "coding": {"provider": "codex_cli", "executable": "codex"}}))
    out = prov.migrate_agents_json()
    assert set(out["migrated"]) == {"coordinator", "coding"} and out["conflicts"] == []
    from aithernet.config import load_config
    nc = load_config(prov.node_config_path())
    assert nc.coordinator.provider == "gemini_cli" and nc.coding_agent.provider == "codex_cli"
    # legacy file renamed so it can never be a second source of truth; idempotent re-run
    assert not (cfgdir / "agents.json").is_file()
    assert (cfgdir / "agents.json.migrated").is_file()
    assert prov.migrate_agents_json()["migrated"] == []


def test_migrate_reports_conflict_without_overwriting(tmp_path):
    # NodeConfig already selects anthropic; legacy agents.json disagrees -> conflict, no overwrite
    prov.configure(prov.COORDINATOR, "anthropic", api_key_ref="ANTHROPIC_API_KEY")
    cfgdir = tmp_path / "config"
    (cfgdir / "agents.json").write_text(json.dumps({
        "coordinator": {"provider": "gemini_cli"}}))
    out = prov.migrate_agents_json()
    assert out["conflicts"] and out["conflicts"][0]["role"] == "coordinator"
    from aithernet.config import load_config
    assert load_config(prov.node_config_path()).coordinator.provider == "anthropic"  # unchanged
