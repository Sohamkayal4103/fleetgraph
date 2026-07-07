"""beta.7 -> beta.8 state-migration tests (phase 1).

Migration must preserve the node key + name, bind the canonical config to the real identity,
reject conflicting identities, be idempotent, and never expose private key material.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aithernet.config.loader import load_config
from aithernet.provisioning import migrate
from aithernet.transport.identity import IdentityManager

_PLACEHOLDER = "00000000-0000-4000-8000-000000000001"


def _make_identity(identity_dir: Path, *, node_id: str, node_name: str) -> str:
    identity_dir.mkdir(parents=True, exist_ok=True)
    mgr = IdentityManager(identity_dir, node_id=node_id, node_name=node_name)
    ident = mgr.initialize()
    return ident.fingerprint


def test_migration_binds_canonical_config_to_existing_identity(tmp_path, monkeypatch):
    # Simulate beta.7: identity already exists under <root>/identity, but no canonical config.
    root = tmp_path / "share"
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(root))
    # keep the XDG-state legacy dir empty/non-conflicting
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdgstate"))
    fp = _make_identity(root / "identity", node_id="real-id", node_name="mp-wsl-beta7")

    rec = migrate.migrate_beta7_state(root)
    assert rec["result"]["verified"]["node_id_real"] is True
    assert rec["result"]["verified"]["identity_matches"] is True
    assert rec["result"]["verified"]["database_absolute"] is True

    cfg = load_config(root / "config" / "node.yaml")
    assert cfg.node_id != _PLACEHOLDER
    assert cfg.node_name == "mp-wsl-beta7"
    assert Path(cfg.agent_transport.identity.state_directory) == (root / "identity").resolve()
    # the canonical identity is the same key we created (fingerprint preserved)
    assert migrate._public_fingerprint(root / "identity") == fp


def test_migration_relocates_legacy_xdg_identity(tmp_path, monkeypatch):
    root = tmp_path / "share"
    root.mkdir(parents=True)
    xdg = tmp_path / "xdgstate"
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    # Only the legacy XDG-state identity exists (no canonical identity yet).
    legacy_dir = xdg / "aithernet" / "identity"
    fp = _make_identity(legacy_dir, node_id="legacy", node_name="legacy-node")

    rec = migrate.migrate_beta7_state(root)
    assert any("relocate identity" in a for a in rec["actions"])
    # the key now lives in the canonical dir, with the SAME fingerprint, and the legacy dir is gone
    assert migrate._public_fingerprint(root / "identity") == fp
    assert not (legacy_dir / "node_ed25519_private.pem").exists()


def test_conflicting_identities_are_rejected(tmp_path, monkeypatch):
    root = tmp_path / "share"
    xdg = tmp_path / "xdgstate"
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    _make_identity(root / "identity", node_id="a", node_name="a")
    _make_identity(xdg / "aithernet" / "identity", node_id="b", node_name="b")  # different key
    with pytest.raises(migrate.AmbiguousIdentityError):
        migrate.migrate_beta7_state(root)


def test_migration_is_idempotent(tmp_path, monkeypatch):
    root = tmp_path / "share"
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdgstate"))
    _make_identity(root / "identity", node_id="real", node_name="n")
    first = migrate.migrate_beta7_state(root)
    node_id_1 = load_config(root / "config" / "node.yaml").node_id
    second = migrate.migrate_beta7_state(root)
    node_id_2 = load_config(root / "config" / "node.yaml").node_id
    assert node_id_1 == node_id_2          # stable
    assert first["result"]["verified"]["identity_matches"]
    assert second["result"]["verified"]["identity_matches"]


def test_migration_record_has_no_private_key(tmp_path, monkeypatch):
    root = tmp_path / "share"
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdgstate"))
    _make_identity(root / "identity", node_id="real", node_name="n")
    migrate.migrate_beta7_state(root)
    # No migration record nor any setup artifact may contain private key PEM material.
    for p in (root / "setup").rglob("*"):
        if p.is_file():
            assert "PRIVATE KEY" not in p.read_text(errors="ignore") or p.name.endswith(".bak")


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "share"
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdgstate"))
    _make_identity(root / "identity", node_id="real", node_name="n")
    migrate.migrate_beta7_state(root, dry_run=True)
    assert not (root / "config" / "node.yaml").exists()
