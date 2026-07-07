"""beta.9 Defect 6: `aithernet doctor --repair` safely repairs customer-level issues
(canonical service config, stale unit, managed provider PATH, setup journal) — non-destructive, no
sudo. Tested against an isolated state root with the service restart suppressed."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aithernet.provisioning import repair, services


@pytest.fixture(autouse=True)
def _isolate_system_units(monkeypatch):
    """Ignore any system-scope unit installed on the build/dev host so installed_scope() reflects
    only the isolated user scope under the test's XDG_CONFIG_HOME."""
    monkeypatch.setattr(services, "system_unit_dirs", lambda: [])


def _state_root(tmp_path: Path, *, coordinator="disabled", executable=None) -> Path:
    root = tmp_path / "state"
    (root / "config").mkdir(parents=True)
    cfg = {
        "node_id": "11111111-1111-4111-8111-111111111111",
        "node_name": "repair-node",
        "node_state_root": str(root),
        "database_url": f"sqlite:///{root}/db/aithernet.db",
        "coordinator": {"provider": coordinator,
                        **({"executable": executable} if executable else {})},
    }
    (root / "config" / "node.yaml").write_text(yaml.safe_dump(cfg))
    return root


def _fake_node_cli(tmp_path: Path, name: str) -> Path:
    binp = tmp_path / ".nvm" / "versions" / "node" / "v20.11.0" / "bin"
    binp.mkdir(parents=True)
    exe = binp / name
    exe.write_text("#!/usr/bin/env node\n")
    exe.chmod(0o755)
    return exe


def test_repair_needs_setup_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    rec = repair.repair_node(tmp_path / "absent", restart=False)
    assert rec["needs_setup"] is True
    assert not rec["changes"]


def test_repair_writes_unit_and_runtime_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    gem = _fake_node_cli(tmp_path, "gemini")
    root = _state_root(tmp_path, coordinator="gemini_cli", executable=str(gem))

    rec = repair.repair_node(root, restart=False)
    assert rec["needs_setup"] is False
    joined = " ".join(rec["changes"])
    assert "reinstall user unit" in joined
    assert "managed runtime PATH" in joined

    # The unit pins the canonical config; the runtime.env carries the provider dir.
    unit = (services.user_unit_dir() / services.UNIT_NAME).read_text()
    assert f"--config {root / 'config' / 'node.yaml'}" in unit
    env = services.runtime_env_file().read_text()
    assert env.startswith("PATH=")
    assert str(gem.parent) in env


def test_repair_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = _state_root(tmp_path)
    repair.repair_node(root, restart=False)
    # A second run finds everything canonical — no unit/PATH rewrite changes.
    rec = repair.repair_node(root, restart=False)
    joined = " ".join(rec["changes"])
    assert "reinstall user unit" not in joined
    assert "managed runtime PATH" not in joined
    checks = " ".join(rec["checks"])
    assert "canonical (--config pinned)" in checks
    assert "managed runtime PATH: current" in checks


def test_repair_dry_run_makes_no_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = _state_root(tmp_path)
    rec = repair.repair_node(root, restart=False, dry_run=True)
    assert rec["dry_run"] is True
    assert rec["changes"]  # it PLANS changes
    # but nothing was actually written
    assert not (services.user_unit_dir() / services.UNIT_NAME).exists()
    assert not services.runtime_env_file().exists()
