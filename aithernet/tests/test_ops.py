"""Stage 14A production-operations tests (temporary dirs + databases; no live deps)."""

from __future__ import annotations

import json
import logging
import os
import socket
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.settings import (
    AgentTransportConfig,
    CoordinatorConfig,
    IdentityConfig,
    MissionExecutionConfig,
    NodeConfig,
    RFBackendsConfig,
    TransportLocalDevelopmentConfig,
)
from aithernet.ops import NodePaths
from aithernet.ops.backup import (
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from aithernet.ops.diagnostics import build_diagnostics, create_diagnostics_bundle, sanitize_config
from aithernet.ops.logging_setup import RedactionFilter, configure_node_logging
from aithernet.ops.preflight import run_preflight
from aithernet.ops.provision import provision_node
from aithernet.ops.readiness import ComponentState, Phase, ReadinessTracker
from aithernet.ops.systemd import render_systemd_unit
from aithernet.ops.upgrade import upgrade_apply, upgrade_preflight, upgrade_rollback, upgrade_verify
from aithernet.orchestrator.runtime import NodeRuntime

NOW = "2026-06-14T00:00:00+00:00"


def _cfg(root: str, node_id: str = "node-aaaa-bbbb", **kw) -> NodeConfig:
    return NodeConfig(
        node_id=node_id, node_name="N", node_state_root=root,
        database_url=f"sqlite:///{root}/db/aithernet.db",
        backup_directory=f"{root}/backups",
        agent_transport=AgentTransportConfig(
            identity=IdentityConfig(state_directory=f"{root}/identity"),
            local_development=TransportLocalDevelopmentConfig(allow_insecure_http=True),
        ),
        **kw,
    )


def _seed_identity(root: str, node_id: str = "node-aaaa-bbbb") -> None:
    idd = Path(root) / "identity"
    idd.mkdir(parents=True, exist_ok=True)
    (idd / "identity.json").write_text(json.dumps({"node_id": node_id, "public_key": "PUB"}))
    (idd / "node_ed25519_private.pem").write_text("-----BEGIN PRIVATE KEY-----\nSECRET\n-----END")


# == provisioning ============================================================================


def test_provision_fresh_node(tmp_path) -> None:
    root = str(tmp_path / "node")
    result = provision_node(root, node_id="n1", node_name="N1")
    assert len(result.created_dirs) >= 11
    assert (Path(root) / "config" / "node.yaml").is_file()
    assert (Path(root) / "db" / "aithernet.db").is_file()
    # identity dir is private (0700)
    assert oct(os.stat(Path(root) / "identity").st_mode & 0o777) == "0o700"


def test_provision_idempotent(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    again = provision_node(root, node_id="n1", node_name="N1")
    assert again.created_dirs == []
    assert any("config exists" in s for s in again.skipped)


def test_provision_preserves_existing_identity_and_db(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    _seed_identity(root)
    marker = Path(root) / "db" / "aithernet.db"
    before = marker.stat().st_mtime
    provision_node(root, node_id="n1", node_name="N1")  # must not recreate
    assert (Path(root) / "identity" / "node_ed25519_private.pem").read_text().startswith("---")
    assert marker.stat().st_mtime >= before  # not deleted/recreated


def test_provision_dry_run_creates_nothing(tmp_path) -> None:
    root = str(tmp_path / "node")
    result = provision_node(root, node_id="n1", node_name="N1", dry_run=True)
    assert result.created_dirs  # reported
    assert not Path(root).exists()  # but nothing written


def _free_port() -> int:
    """Reserve, then release, an ephemeral port so the preflight port check sees it free.

    The default config port (8080) may legitimately be bound on the host (a running node /
    hosted stack), which is exercised separately by ``test_preflight_reports_port_conflict``.
    Cwd-independence must not depend on that global, so pin a port known to be free here.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_preflight_is_working_directory_independent(tmp_path, monkeypatch) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)  # run from a different CWD
    report = run_preflight(_cfg(root, node_id="n1", port=_free_port()))
    assert report.ok  # resolves absolute paths regardless of CWD


# == configuration validation ================================================================


def test_preflight_reports_missing_executable(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    cfg = _cfg(root, node_id="n1",
               coordinator=CoordinatorConfig(provider="gemini_cli",
                                             executable="definitely-not-on-path-xyz"))
    report = run_preflight(cfg)
    coord = next(c for c in report.checks if c.name == "coordinator_executable")
    assert coord.status == "warn" and "not on PATH" in coord.detail


def test_preflight_reports_unwritable_database_dir(tmp_path) -> None:
    cfg = NodeConfig(node_id="n1", node_name="N",
                     database_url="sqlite:////nonexistent-root-xyz/db/x.db")
    report = run_preflight(cfg)
    db = next(c for c in report.checks if c.name == "database_directory")
    assert db.status == "fail"


def test_preflight_reports_port_conflict(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    try:
        cfg = _cfg(root, node_id="n1", port=port)
        report = run_preflight(cfg)
        portcheck = next(c for c in report.checks if c.name == "port_availability")
        assert portcheck.status == "fail"
    finally:
        sock.close()


def test_preflight_flags_unsafe_dev_path(tmp_path) -> None:
    # A /tmp database is flagged as unsafe for production.
    cfg = NodeConfig(node_id="n1", node_name="N", database_url="sqlite:////tmp/aithernet-x/x.db")
    report = run_preflight(cfg)
    assert any(c.name == "database_path_safety" and c.status == "warn" for c in report.checks)


# == readiness ===============================================================================


def test_readiness_migrations_gate_then_ready() -> None:
    r = ReadinessTracker()
    r.register("migrations", required=True)
    r.register("transport_worker", required=True)
    r.set_phase(Phase.READY)
    assert not r.is_ready()  # components still pending
    r.mark("migrations", ComponentState.READY)
    assert not r.is_ready()
    r.mark("transport_worker", ComponentState.READY)
    assert r.is_ready()


def test_readiness_optional_failure_degrades_but_stays_ready() -> None:
    r = ReadinessTracker()
    r.register("migrations", required=True)
    r.register("rf_default_backend", required=False)  # experimental/optional
    r.set_phase(Phase.READY)
    r.mark("migrations", ComponentState.READY)
    r.mark("rf_default_backend", ComponentState.FAILED)
    assert r.is_ready()  # optional failure never makes the node unready
    assert r.phase is Phase.DEGRADED


def test_readiness_required_backend_failure_blocks_ready() -> None:
    r = ReadinessTracker()
    r.register("migrations", required=True)
    r.register("rf_default_backend", required=True)  # require_default_ready=True
    r.set_phase(Phase.READY)
    r.mark("migrations", ComponentState.READY)
    r.mark("rf_default_backend", ComponentState.FAILED)
    assert not r.is_ready()


def test_health_endpoints_live_and_ready(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    runtime = NodeRuntime.from_config(
        _cfg(root, node_id="n1", mission_execution=MissionExecutionConfig(enabled=False))
    )
    with TestClient(create_app(runtime=runtime)) as client:
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        body = ready.json()
        assert body["status"] == "ready"
        assert any(c["name"] == "migrations" and c["state"] == "ready"
                   for c in body["components"])


# == backup / restore ========================================================================


def _provisioned_cfg(tmp_path):
    root = str(tmp_path / "node")
    provision_node(root, node_id="node-aaaa-bbbb", node_name="N")
    _seed_identity(root)
    return root, _cfg(root)


def test_backup_is_consistent_and_checksummed(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    result = create_backup(cfg, backup_dir=f"{root}/backups",
                           config_path=f"{root}/config/node.yaml", now_iso=NOW)
    assert "database.sqlite3" in result.files
    assert verify_backup(result.path).ok


def test_backup_excludes_private_identity_by_default(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    result = create_backup(cfg, backup_dir=f"{root}/backups", now_iso=NOW)
    with tarfile.open(result.path) as tar:
        names = tar.getnames()
    assert "identity/node_ed25519_private.pem" not in names
    assert "identity/identity.json" in names


def test_corrupt_backup_rejected(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    result = create_backup(cfg, backup_dir=f"{root}/backups", now_iso=NOW)
    data = Path(result.path).read_bytes()
    bad = Path(result.path + ".bad")
    bad.write_bytes(data[:-40] + b"X" * 40)
    assert not verify_backup(bad).ok


def test_restore_dry_run_changes_nothing(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    result = create_backup(cfg, backup_dir=f"{root}/backups",
                           config_path=f"{root}/config/node.yaml", now_iso=NOW)
    rr = restore_backup(result.path, cfg, config_path=f"{root}/config/node.yaml",
                        dry_run=True, now_iso=NOW)
    assert rr.ok and rr.rollback_snapshot is None and rr.restored_files


def test_restore_preserves_rollback_snapshot(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    result = create_backup(cfg, backup_dir=f"{root}/backups",
                           config_path=f"{root}/config/node.yaml", now_iso=NOW)
    rr = restore_backup(result.path, cfg, config_path=f"{root}/config/node.yaml",
                        dry_run=False, now_iso="2026-06-14T01:00:00+00:00")
    assert rr.ok and rr.rollback_snapshot and Path(rr.rollback_snapshot).is_file()


def test_restore_refuses_identity_mismatch(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    result = create_backup(cfg, backup_dir=f"{root}/backups", now_iso=NOW)
    other = _cfg(root, node_id="DIFFERENT-NODE-ID")
    rr = restore_backup(result.path, other, config_path=None, dry_run=True, now_iso=NOW)
    assert not rr.ok and any("mismatch" in i for i in rr.issues)


def test_list_backups(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    create_backup(cfg, backup_dir=f"{root}/backups", now_iso=NOW)
    rows = list_backups(f"{root}/backups")
    assert len(rows) == 1 and rows[0]["node_id"] == "node-aaaa-bbbb"


# == upgrade =================================================================================


def test_upgrade_preflight_and_verify(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    pf = upgrade_preflight(cfg)
    assert pf["ready_to_apply"] is True
    assert pf["migration_state"]["schema_valid"] is True
    assert pf["migration_state"]["pending"] == []
    assert upgrade_verify(cfg)["ok"] is True


def test_upgrade_preflight_ready_when_node_api_port_is_bound(tmp_path) -> None:
    # An upgrade is normally run against a RUNNING node, so its API port is already bound. The
    # port check is still surfaced (FAIL) for the operator, but readiness to apply migrations is
    # gated on the database/config only, so a live node can be preflighted and upgraded.
    root, _ = _provisioned_cfg(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    try:
        cfg = _cfg(root, port=port)
        pf = upgrade_preflight(cfg)
        portcheck = next(c for c in pf["preflight"]["checks"] if c["name"] == "port_availability")
        assert portcheck["status"] == "fail"          # surfaced...
        assert pf["ready_to_apply"] is True            # ...but does not block the DB upgrade
        assert pf["blocking_checks"] == []
    finally:
        sock.close()


def test_upgrade_preflight_fails_closed_on_malformed_db(tmp_path) -> None:
    # A database whose schema is broken (a current table dropped) with no pending migration to
    # restore it must NOT be reported ready to apply.
    from sqlalchemy import text

    from aithernet.state.db import create_db_engine
    root, cfg = _provisioned_cfg(tmp_path)
    engine = create_db_engine(cfg.database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE events"))
    engine.dispose()
    pf = upgrade_preflight(cfg)
    assert pf["migration_state"]["schema_valid"] is False
    assert pf["ready_to_apply"] is False
    assert "migration_state" in pf["blocking_checks"]


def test_upgrade_preflight_is_read_only(tmp_path) -> None:
    # Read-only: preflighting a valid node database does not change its bytes.
    import hashlib
    root, cfg = _provisioned_cfg(tmp_path)
    db = Path(root) / "db" / "aithernet.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    upgrade_preflight(cfg)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_upgrade_apply_creates_rollback_backup(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    result = upgrade_apply(cfg, config_path=f"{root}/config/node.yaml",
                           backup_dir=f"{root}/backups", now_iso=NOW)
    assert result["ok"] and Path(result["rollback_backup"]).is_file()
    # the rollback backup is itself valid
    assert verify_backup(result["rollback_backup"]).ok


def test_upgrade_rollback_restores(tmp_path) -> None:
    root, cfg = _provisioned_cfg(tmp_path)
    backup = create_backup(cfg, backup_dir=f"{root}/backups",
                           config_path=f"{root}/config/node.yaml", now_iso=NOW)
    out = upgrade_rollback(cfg, backup_path=backup.path,
                           config_path=f"{root}/config/node.yaml", now_iso=NOW)
    assert out["ok"]


# == diagnostics + logging ===================================================================


def test_diagnostics_sanitizes_secrets(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="node-zzz", node_name="Z")
    cfg = _cfg(root, node_id="node-zzz",
               coordinator=CoordinatorConfig(provider="openai_compatible",
                                             base_url="http://x", api_key="SUPER-SECRET-KEY"))
    sanitized = sanitize_config(cfg)
    assert sanitized["coordinator"]["api_key"] == "<redacted>"
    info = build_diagnostics(cfg, now_iso=NOW)
    assert "SUPER-SECRET-KEY" not in json.dumps(info)
    assert info["database_integrity"] == "ok"


def test_diagnostics_bundle_has_no_secrets_or_db_contents(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="node-zzz", node_name="Z")
    cfg = _cfg(root, node_id="node-zzz",
               coordinator=CoordinatorConfig(provider="openai_compatible",
                                             base_url="http://x", api_key="SUPER-SECRET-KEY"))
    bundle = create_diagnostics_bundle(cfg, output_dir=f"{root}/diag", now_iso=NOW)
    blob = Path(bundle).read_bytes()
    assert b"SUPER-SECRET-KEY" not in blob
    # the bundle has no raw database file
    with tarfile.open(bundle) as tar:
        assert "database.sqlite3" not in tar.getnames()


def test_logging_redacts_keys_and_signatures(tmp_path) -> None:
    log_file = tmp_path / "node.log"
    configure_node_logging("node-1", log_file=str(log_file))
    log = logging.getLogger("test.secret")
    log.warning("private key -----BEGIN PRIVATE KEY-----\nSECRETKEYBYTES\n"
                "-----END PRIVATE KEY-----")
    log.warning('payload {"signature": "abcd1234signaturebytes"} done')
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text()
    assert "SECRETKEYBYTES" not in text
    assert "abcd1234signaturebytes" not in text
    assert "<redacted>" in text


def test_redaction_filter_unit() -> None:
    f = RedactionFilter()
    rec = logging.LogRecord("c", logging.INFO, __file__, 1, "key sk-ABCDEFGH12345678 end", (), None)
    f.filter(rec)
    assert "sk-ABCDEFGH12345678" not in rec.getMessage()


# == systemd =================================================================================


def test_systemd_unit_renders_hardened(tmp_path) -> None:
    unit = render_systemd_unit(node_id="n1", state_root=str(tmp_path / "node"))
    assert "KillMode=mixed" in unit          # reap subprocess tree (no orphans)
    assert "TimeoutStopSec=45" in unit        # bounded graceful stop
    assert "EnvironmentFile=" in unit         # secrets via env file, not the command line
    assert "Restart=on-failure" in unit
    assert "ReadWritePaths=" in unit


# == API ops surface =========================================================================


def test_ops_api_endpoints(tmp_path) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    runtime = NodeRuntime.from_config(
        _cfg(root, node_id="n1", mission_execution=MissionExecutionConfig(enabled=False))
    )
    with TestClient(create_app(runtime=runtime)) as client:
        ov = client.get("/ops/overview").json()
        assert ov["version"] and ov["migrations"]["schema_valid"] is True
        pf = client.get("/ops/preflight").json()
        assert "checks" in pf
        assert client.get("/ops/backups").status_code == 200
        diag = client.get("/ops/diagnostics").json()
        assert diag["database_integrity"] == "ok"
        assert "SUPER-SECRET" not in json.dumps(diag)


def test_node_paths_layout(tmp_path) -> None:
    from aithernet.ops.paths import LAYOUT
    paths = NodePaths.from_root(tmp_path / "node")
    assert paths.identity_dir.name == "identity"
    assert paths.database_url.startswith("sqlite:///")
    assert ("identity_dir", "identity", 0o700) in LAYOUT


@pytest.mark.parametrize("require", [True, False])
def test_required_rf_backend_flag_wires_readiness(tmp_path, require) -> None:
    root = str(tmp_path / "node")
    provision_node(root, node_id="n1", node_name="N1")
    cfg = _cfg(root, node_id="n1", mission_execution=MissionExecutionConfig(enabled=False),
               rf_backends=RFBackendsConfig(require_default_ready=require))
    runtime = NodeRuntime.from_config(cfg)
    comp = next(c for c in runtime.readiness.components.values()
                if c.name == "rf_default_backend")
    assert comp.required is require
