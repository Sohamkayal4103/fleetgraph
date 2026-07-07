"""Stage 13B schema-migration tests (temporary SQLite files).

The production failure was an older ``aithernet.db`` whose ``external_agents`` table predates
the Stage 13A/13B columns: ``create_all()`` had created the newer *tables* but never added the
newer *columns*, so ``aithernet peer list`` / ``transport status`` returned HTTP 500
(``no such column: external_agents.role``). These tests build databases at earlier schema
versions, run the migration framework, and assert it upgrades them additively, idempotently,
and without losing data — and that the previously-failing peer/transport endpoints then work.

Historical tables are reconstructed by cloning the *current* model table minus the columns a
given stage had not yet introduced (and minus indexes that reference those columns). This
mirrors exactly what an older schema looked like, using the real column types/defaults.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, text
from sqlalchemy import Table as SATable

from aithernet.api.app import create_app
from aithernet.config.settings import (
    AgentTransportConfig,
    IdentityConfig,
    NodeConfig,
    TransportLocalDevelopmentConfig,
)
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.state.db import create_db_engine
from aithernet.state.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    migration_status,
    run_migrations,
    validate_schema,
)
from aithernet.state.models import Base

# Column groups each stage introduced (must mirror the migration definitions).
A13_PEER_COLS = [
    "role", "expected_node_id", "public_key", "fingerprint", "trust_state", "enabled",
    "tls_verify", "last_success_at", "last_failure_at", "consecutive_failures",
    "last_manifest_json", "capability_snapshot_json", "capability_snapshot_at", "last_error",
]
B_AUTH_COLS = [
    "may_send_requests", "may_send_replies", "may_receive_messages", "may_request_response",
    "max_inbound_request_bytes", "max_concurrent_inbound_missions",
]
A5_MCP_COLS = [
    "backend_id", "backend_kind", "backend_version", "backend_source_revision",
    "result_summary_type",
]
B_OUTBOX_APP_COLS = [
    "application_type", "application_request_id", "reply_to_request_id", "expects_reply",
]
B_INBOX_APP_COLS = [
    "application_type", "application_request_id", "reply_to_request_id", "application_state",
]
# Stage 13D.2 per-peer artifact authorization columns (none of the earlier stages had them).
B2_ARTIFACT_PERM_COLS = [
    "may_offer_artifacts", "may_request_artifacts", "may_receive_artifacts",
    "max_artifact_bytes", "max_concurrent_transfers", "allowed_artifact_kinds_json",
]
# Stage 13D.3 per-peer mission-status authorization columns (none of the earlier stages had them).
B3_STATUS_PERM_COLS = [
    "may_publish_mission_status", "may_query_mission_status",
    "may_receive_mission_status", "max_active_remote_snapshots",
]
# All post-13B external_agents columns absent from any pre-13B historical schema.
LATER_EA_COLS = B2_ARTIFACT_PERM_COLS + B3_STATUS_PERM_COLS

CORE_TABLES = [
    "missions", "events", "mission_steps", "mission_execution_runs", "coding_tasks",
    "coding_task_results", "mcp_tool_calls", "gnuradio_context", "external_agents",
    "external_agent_messages",
]


# -- historical-schema builders --------------------------------------------------------------


def _create_legacy_table(conn, table_name: str, drop_cols: set[str]) -> None:
    """Create ``table_name`` as an OLDER schema: the current table minus ``drop_cols``.

    The copied columns retain their ``index=True``/``unique=True`` flags, so each table's
    single-column indexes are regenerated automatically; an index on a dropped column simply
    is not created (its column is absent). This mirrors what an older schema looked like using
    the real column types/defaults."""
    src = Base.metadata.tables[table_name]
    md = MetaData()
    columns = [col._copy() for col in src.columns if col.name not in drop_cols]
    SATable(table_name, md, *columns).create(bind=conn)


def _ts() -> str:
    return "2026-01-01T00:00:00+00:00"


def seed_stage1(path: str) -> None:
    """A Stage-1 database: only ``missions`` and ``events`` exist, with one row each."""
    engine = create_db_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        _create_legacy_table(conn, "missions", drop_cols=set())
        _create_legacy_table(conn, "events", drop_cols=set())
        conn.execute(text(
            "INSERT INTO missions (id, source_type, content, status, created_at, updated_at, "
            "metadata_json) VALUES ('m1', 'user', 'hello', 'received', :t, :t, '{}')"
        ), {"t": _ts()})
        conn.execute(text(
            "INSERT INTO events (id, event_type, source, message, payload_json, created_at) "
            "VALUES ('e1', 'mission.received', 'runtime', 'hi', '{}', :t)"
        ), {"t": _ts()})
    engine.dispose()


def seed_pre_13a(path: str) -> None:
    """A pre-Stage-13A database: Stage-7 ``external_agents`` (no peer/trust/auth columns),
    pre-13A.5 ``mcp_tool_calls`` (no backend columns), and one connected external agent row."""
    engine = create_db_engine(f"sqlite:///{path}")
    drop_ea = set(A13_PEER_COLS) | set(B_AUTH_COLS) | set(LATER_EA_COLS)
    with engine.begin() as conn:
        for tname in CORE_TABLES:
            drop = set()
            if tname == "external_agents":
                drop = drop_ea
            elif tname == "mcp_tool_calls":
                drop = set(A5_MCP_COLS)
            _create_legacy_table(conn, tname, drop)
        conn.execute(text(
            "INSERT INTO missions (id, source_type, content, status, created_at, updated_at, "
            "metadata_json) VALUES ('m1', 'user', 'hello', 'received', :t, :t, '{}')"
        ), {"t": _ts()})
        conn.execute(text(
            "INSERT INTO events (id, event_type, source, message, payload_json, created_at) "
            "VALUES ('e1', 'mission.received', 'runtime', 'hi', '{}', :t)"
        ), {"t": _ts()})
        conn.execute(text(
            "INSERT INTO external_agents (id, name, agent_type, transport, status, "
            "metadata_json, created_at, updated_at) VALUES "
            "('peer-1', 'Legacy Peer', 'node', 'http', 'connected', '{}', :t, :t)"
        ), {"t": _ts()})
    engine.dispose()


def seed_stage_13a(path: str) -> None:
    """A Stage-13A database: ``external_agents`` has peer/trust columns but NOT the Stage-13B
    auth columns; inbox/outbox tables exist without the Stage-13B application columns; no RF."""
    engine = create_db_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        for tname in CORE_TABLES:
            drop = set()
            if tname == "external_agents":
                drop = set(B_AUTH_COLS) | set(LATER_EA_COLS)
            elif tname == "mcp_tool_calls":
                drop = set(A5_MCP_COLS)
            _create_legacy_table(conn, tname, drop)
        _create_legacy_table(conn, "agent_outbox_messages", set(B_OUTBOX_APP_COLS))
        _create_legacy_table(conn, "agent_inbox_messages", set(B_INBOX_APP_COLS))
        conn.execute(text(
            "INSERT INTO external_agents (id, name, agent_type, transport, status, "
            "metadata_json, created_at, updated_at, role, trust_state, enabled, tls_verify, "
            "consecutive_failures) VALUES ('peer-1', 'Trusted Node', 'node', 'http', "
            "'connected', '{}', :t, :t, 'node', 'trusted', 1, 1, 0)"
        ), {"t": _ts()})
    engine.dispose()


def seed_stage_13a5(path: str) -> None:
    """A Stage-13A.5 database: RF tables + ``mcp_tool_calls`` backend columns present, but
    still missing the Stage-13B auth/application columns and the Stage-13B tables."""
    engine = create_db_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        for tname in CORE_TABLES:
            drop = set()
            if tname == "external_agents":
                drop = set(B_AUTH_COLS) | set(LATER_EA_COLS)
            _create_legacy_table(conn, tname, drop)
        _create_legacy_table(conn, "agent_outbox_messages", set(B_OUTBOX_APP_COLS))
        _create_legacy_table(conn, "agent_inbox_messages", set(B_INBOX_APP_COLS))
        for tname in ("rf_workspace_context", "rf_artifacts", "rf_benchmark_runs"):
            _create_legacy_table(conn, tname, set())
        conn.execute(text(
            "INSERT INTO mcp_tool_calls (id, caller, tool_name, status, arguments_json, "
            "result_json, backend_id, created_at) VALUES ('c1', 'user', 'list_blocks', "
            "'completed', '{}', '{}', 'legacy_gr_mcp', :t)"
        ), {"t": _ts()})
    engine.dispose()


# -- helpers ---------------------------------------------------------------------------------


def _columns(path: str, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _indexes(path: str, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


def _tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _count(path: str, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _migrate(path: str):
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        return run_migrations(engine)
    finally:
        engine.dispose()


def _validate(path: str):
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        return validate_schema(engine)
    finally:
        engine.dispose()


# == 1. fresh empty database =================================================================


def test_fresh_database_migrates_to_valid(tmp_path) -> None:
    path = str(tmp_path / "fresh.db")
    report = _migrate(path)
    assert report.applied_now == [m.id for m in MIGRATIONS]
    assert _validate(path).ok


def test_fresh_database_behavior_unchanged(tmp_path) -> None:
    # A fresh database ends up with exactly the current metadata tables (+ history table).
    path = str(tmp_path / "fresh.db")
    _migrate(path)
    assert set(Base.metadata.tables) | {"schema_migrations"} == _tables(path)


# == 2. database with only early Stage 1 tables ==============================================


def test_stage1_database_upgrades_and_preserves_rows(tmp_path) -> None:
    path = str(tmp_path / "s1.db")
    seed_stage1(path)
    assert _tables(path) == {"missions", "events"}
    _migrate(path)
    assert _validate(path).ok
    assert _count(path, "missions") == 1 and _count(path, "events") == 1


# == 3. database with a pre-Stage-13A external_agents table ==================================


def test_pre_13a_adds_all_peer_columns(tmp_path) -> None:
    path = str(tmp_path / "pre13a.db")
    seed_pre_13a(path)
    before = _columns(path, "external_agents")
    assert "role" not in before  # reproduces the production failure shape
    _migrate(path)
    after = _columns(path, "external_agents")
    for col in A13_PEER_COLS + B_AUTH_COLS:
        assert col in after, col
    assert "ix_external_agents_fingerprint" in _indexes(path, "external_agents")
    assert _validate(path).ok


def test_pre_13a_preserves_external_agent_rows(tmp_path) -> None:
    path = str(tmp_path / "pre13a.db")
    seed_pre_13a(path)
    assert _count(path, "external_agents") == 1
    _migrate(path)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT id, name, role, trust_state, may_send_requests, may_send_replies, "
            "may_receive_messages, may_request_response FROM external_agents WHERE id='peer-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "peer-1" and row[1] == "Legacy Peer"
    # Conservative defaults: trust must not grant inbound-request authorization.
    assert row[2] == "unknown" and row[3] == "untrusted"
    assert row[4] == 0  # may_send_requests defaults false
    assert row[5] == 1  # may_send_replies defaults true (Stage 13B design)
    assert row[6] == 1  # may_receive_messages defaults true
    assert row[7] == 0  # may_request_response defaults false


# == 4. Stage 13A database upgrading to Stage 13B ============================================


def test_stage_13a_to_13b_adds_auth_and_application_columns(tmp_path) -> None:
    path = str(tmp_path / "s13a.db")
    seed_stage_13a(path)
    assert "may_send_requests" not in _columns(path, "external_agents")
    assert "application_request_id" not in _columns(path, "agent_outbox_messages")
    _migrate(path)
    for col in B_AUTH_COLS:
        assert col in _columns(path, "external_agents")
    for col in B_OUTBOX_APP_COLS:
        assert col in _columns(path, "agent_outbox_messages")
    for col in B_INBOX_APP_COLS:
        assert col in _columns(path, "agent_inbox_messages")
    assert _validate(path).ok


def test_stage_13a_preserves_trust_and_uses_conservative_auth_defaults(tmp_path) -> None:
    path = str(tmp_path / "s13a.db")
    seed_stage_13a(path)
    _migrate(path)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT trust_state, may_send_requests, may_request_response, may_send_replies "
            "FROM external_agents WHERE id='peer-1'"
        ).fetchone()
    finally:
        conn.close()
    # An already-TRUSTED transport peer keeps its trust but gains NO inbound authorization.
    assert row[0] == "trusted"
    assert row[1] == 0 and row[2] == 0 and row[3] == 1


# == 5. Stage 13A.5 database upgrading to Stage 13B =========================================


def test_stage_13a5_to_13b_creates_comms_tables_and_keeps_rf(tmp_path) -> None:
    path = str(tmp_path / "s13a5.db")
    seed_stage_13a5(path)
    assert "mission_reply_waits" not in _tables(path)
    assert "inbound_requests" not in _tables(path)
    _migrate(path)
    tables = _tables(path)
    assert {"mission_reply_waits", "inbound_requests", "rf_artifacts"} <= tables
    assert _count(path, "mcp_tool_calls") == 1  # RF-era row preserved
    assert _validate(path).ok


# == 6-13. element coverage (mostly exercised above; explicit index + table checks) ==========


def test_stage_13a_transport_tables_exist_after_pre_13a_migration(tmp_path) -> None:
    path = str(tmp_path / "pre13a.db")
    seed_pre_13a(path)
    assert "agent_inbox_messages" not in _tables(path)
    _migrate(path)
    assert {"agent_inbox_messages", "agent_outbox_messages"} <= _tables(path)


def test_rf_tables_and_mcp_backend_columns_created_from_pre_13a(tmp_path) -> None:
    path = str(tmp_path / "pre13a.db")
    seed_pre_13a(path)
    _migrate(path)
    assert {"rf_workspace_context", "rf_artifacts", "rf_benchmark_runs"} <= _tables(path)
    for col in A5_MCP_COLS:
        assert col in _columns(path, "mcp_tool_calls")
    assert "ix_mcp_tool_calls_backend_id" in _indexes(path, "mcp_tool_calls")


def test_required_indexes_created(tmp_path) -> None:
    path = str(tmp_path / "pre13a.db")
    seed_pre_13a(path)
    _migrate(path)
    # Every current-metadata index exists; validation covers this comprehensively.
    v = _validate(path)
    assert v.missing_indexes == [] and v.ok


# == 14. repeated migration is idempotent ====================================================


def test_repeated_migration_is_idempotent(tmp_path) -> None:
    path = str(tmp_path / "pre13a.db")
    seed_pre_13a(path)
    first = _migrate(path)
    assert first.applied_now  # applied a batch
    second = _migrate(path)
    assert second.applied_now == []  # nothing left to apply
    assert second.already_applied == [m.id for m in MIGRATIONS]
    assert _validate(path).ok


# == 15-16. partial application recovers; no history row recorded after failure ==============


def test_partial_application_recovers_and_failure_records_nothing(tmp_path) -> None:
    path = str(tmp_path / "pre13a.db")
    seed_pre_13a(path)

    # A poisoned migration list: 0001 + 0002 succeed, then a deliberately failing migration.
    def _boom(conn):
        conn.execute(text("ALTER TABLE does_not_exist ADD COLUMN x INTEGER"))

    poisoned = MIGRATIONS[:2] + [Migration("0099_boom", "fails on purpose", _boom)]
    engine = create_db_engine(f"sqlite:///{path}")
    import aithernet.state.migrations as mig

    original = mig.MIGRATIONS
    try:
        mig.MIGRATIONS = poisoned
        with pytest.raises(MigrationError) as excinfo:
            run_migrations(engine)
        # Sanitized: only the migration id + error type, never SQL/rows.
        assert excinfo.value.migration_id == "0099_boom"
        assert "boom" in str(excinfo.value)
        # The two good migrations committed; the failing one recorded NOTHING.
        with engine.begin() as conn:
            applied = {
                r[0] for r in conn.execute(text("SELECT migration_id FROM schema_migrations"))
            }
        assert applied == {"0001_core_baseline", "0002_transport_peer_columns"}
        assert "0099_boom" not in applied
        # Recovery: restore the real list and finish — the two applied ones are skipped.
        mig.MIGRATIONS = original
        report = run_migrations(engine)
        assert "0001_core_baseline" in report.already_applied
        assert "0005_comms_peer_authorization" in report.applied_now
    finally:
        mig.MIGRATIONS = original
        engine.dispose()
    assert _validate(path).ok


def test_failed_migration_records_no_history_then_idempotently_recovers(tmp_path) -> None:
    # pysqlite implicitly commits before each DDL statement, so a column added before a
    # mid-migration failure may persist. The guarantee that matters is NOT DDL rollback but
    # (a) no history row for the failed id and (b) idempotent recovery: a re-run adds only
    # what is still missing and then records the id.
    path = str(tmp_path / "partial.db")
    seed_pre_13a(path)

    state = {"explode": True}

    def _flaky(conn):
        # First pass: add one real column, then fail. Second pass: succeed (idempotent).
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(external_agents)"))}
        if "role" not in existing:
            conn.execute(text("ALTER TABLE external_agents ADD COLUMN role VARCHAR(32) "
                              "NOT NULL DEFAULT 'unknown'"))
        if state["explode"]:
            raise RuntimeError("simulated mid-migration failure")

    engine = create_db_engine(f"sqlite:///{path}")
    import aithernet.state.migrations as mig

    original = mig.MIGRATIONS
    try:
        mig.MIGRATIONS = [Migration("0001_core_baseline", "core", original[0].apply),
                          Migration("0099_flaky", "fails then recovers", _flaky)]
        with pytest.raises(MigrationError) as excinfo:
            run_migrations(engine)
        assert excinfo.value.migration_id == "0099_flaky"
        with engine.begin() as conn:
            applied = {r[0] for r in conn.execute(text("SELECT migration_id FROM "
                                                       "schema_migrations"))}
        # No history row for the failed migration; the column it partly added may persist.
        assert "0099_flaky" not in applied

        # Recovery: the same idempotent migration now succeeds and is recorded exactly once.
        state["explode"] = False
        report = run_migrations(engine)
        assert "0099_flaky" in report.applied_now
        with engine.begin() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM schema_migrations WHERE "
                                      "migration_id='0099_flaky'")).scalar()
        assert count == 1
    finally:
        mig.MIGRATIONS = original
        engine.dispose()


# == 17-20. migration runs before repositories query; previously-failing endpoints work ======


def _runtime_on(tmp_path, db_path: str) -> NodeRuntime:
    config = NodeConfig(
        node_id="00000000-0000-4000-8000-0000000000aa",
        node_name="migration-node",
        database_url=f"sqlite:///{db_path}",
        agent_transport=AgentTransportConfig(
            identity=IdentityConfig(state_directory=str(tmp_path / "identity")),
            local_development=TransportLocalDevelopmentConfig(allow_insecure_http=True),
        ),
    )
    return NodeRuntime.from_config(config)  # runs migrations before any repository is used


def test_peer_and_transport_endpoints_succeed_after_migration(tmp_path) -> None:
    # This is the exact production failure: a pre-13A external_agents table -> 500s.
    db_path = str(tmp_path / "prod_like.db")
    seed_pre_13a(db_path)
    runtime = _runtime_on(tmp_path, db_path)
    with TestClient(create_app(runtime=runtime)) as client:
        peers = client.get("/peers")
        assert peers.status_code == 200  # was 500: no such column: external_agents.role
        # the migrated legacy peer is listed
        assert any(p.get("name") == "Legacy Peer" for p in peers.json())
        assert client.get("/agent-transport/status").status_code == 200


def test_conversation_and_inbound_request_apis_work_after_migration(tmp_path) -> None:
    db_path = str(tmp_path / "prod_like.db")
    seed_pre_13a(db_path)
    runtime = _runtime_on(tmp_path, db_path)
    with TestClient(create_app(runtime=runtime)) as client:
        assert client.get("/conversations").status_code == 200
        assert client.get("/inbound-requests").status_code == 200
        assert client.get("/missions").status_code == 200


def test_migration_runs_before_peer_query_in_from_config(tmp_path) -> None:
    # If migrations did NOT run first, constructing the runtime + querying peers would raise.
    db_path = str(tmp_path / "ordering.db")
    seed_pre_13a(db_path)
    runtime = _runtime_on(tmp_path, db_path)
    # A direct repository query through the runtime session succeeds (columns present).
    from aithernet.state.repositories import PeerRepository

    with runtime.session_scope() as session:
        peers = PeerRepository(session).list()
    assert len(peers) == 1


# == 21-23. fresh unchanged; unknown extras preserved; no user-data loss ====================


def test_unknown_extra_columns_are_preserved(tmp_path) -> None:
    path = str(tmp_path / "extra.db")
    seed_pre_13a(path)
    engine = create_db_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE external_agents ADD COLUMN operator_note TEXT"))
        conn.execute(text("UPDATE external_agents SET operator_note='keep me'"))
    engine.dispose()
    _migrate(path)
    assert "operator_note" in _columns(path, "external_agents")
    conn = sqlite3.connect(path)
    try:
        note = conn.execute(
            "SELECT operator_note FROM external_agents WHERE id='peer-1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert note == "keep me"
    # Validation does not flag/remove the unknown extra column.
    assert _validate(path).ok


def test_no_user_data_rows_lost_across_full_upgrade(tmp_path) -> None:
    path = str(tmp_path / "data.db")
    seed_pre_13a(path)
    before = {t: _count(path, t) for t in ("missions", "events", "external_agents")}
    _migrate(path)
    after = {t: _count(path, t) for t in ("missions", "events", "external_agents")}
    assert before == after == {"missions": 1, "events": 1, "external_agents": 1}


# == status helper ===========================================================================


def test_migration_status_reports_history_and_validation(tmp_path) -> None:
    path = str(tmp_path / "status.db")
    seed_pre_13a(path)
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        before = migration_status(engine)
        assert before.pending == [m.id for m in MIGRATIONS]
        assert before.validation.ok is False
        run_migrations(engine)
        after = migration_status(engine)
        assert after.pending == []
        assert after.validation.ok is True
        assert len(after.applied) == len(MIGRATIONS)
    finally:
        engine.dispose()
