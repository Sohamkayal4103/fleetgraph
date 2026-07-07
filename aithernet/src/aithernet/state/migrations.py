"""Deterministic, idempotent, additive schema migrations for the node state store.

``Base.metadata.create_all()`` creates missing *tables* but never adds *columns* to an
existing SQLite table, so a database created by an older Aithernet schema is structurally
outdated even though the application starts (it then fails the moment a repository queries a
newer column — e.g. ``no such column: external_agents.role``). This module owns an ordered
set of additive migrations that bring **any** earlier database up to the current
``Base.metadata`` exactly once, non-destructively, before any repository runs a query.

Design invariants:

* **Additive only** — migrations never drop a table, column, or row, never rewrite user
  data, and never delete the database. Unknown extra columns from a newer/forked schema are
  left in place.
* **Idempotent** — every migration inspects the real schema before changing it, so a
  partially applied migration (or a fresh database already at the current schema) is safe to
  (re-)run. Adding a column that already exists is skipped, not retried into an error.
* **Ordered + recorded** — each migration id is recorded in ``schema_migrations`` only after
  its DDL succeeds, in the same transaction as the history insert; a migration that raises
  records nothing and is retried on the next run. NOTE: the pysqlite driver issues an implicit
  ``COMMIT`` before each DDL statement, so an individual ``ALTER TABLE ADD COLUMN`` / ``CREATE
  TABLE`` cannot be relied on to roll back mid-migration. Safe recovery therefore rests on
  *idempotency*, not DDL atomicity: every migration inspects the live schema first, so a
  re-run after a partial failure adds only what is still missing and then records the id. The
  history insert is an ordinary row write and is never committed when the migration raises, so
  a failed migration is always retried in full.
* **Conservative defaults** — new ``NOT NULL`` columns are added with the exact constant
  default declared on the current model, which SQLite back-fills into existing rows. In
  particular a trusted transport peer is *not* granted inbound coordinator authorization:
  ``may_send_requests``/``may_request_response`` default false; reply acceptance follows the
  Stage 13B model defaults.
* **Sanitized errors** — a failure surfaces only the migration id and the error *type*; it
  never includes SQL parameters, row values, keys, signatures, prompts, or environment.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, inspect, text

from aithernet.state.models import Base, utcnow

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"


# -- low-level, dialect-aware, idempotent helpers --------------------------------------------


def _table_exists(conn: Connection, name: str) -> bool:
    return inspect(conn).has_table(name)


def _existing_columns(conn: Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {col["name"] for col in inspect(conn).get_columns(table)}


def _scalar_default_sql(column) -> str | None:
    """Render a column's *constant* Python-side default as a SQL literal, or ``None``.

    Callable defaults (``dict``/``list``/``utcnow``/``new_uuid``) are not constants and yield
    ``None`` — none of the additive ``NOT NULL`` columns use one. Booleans render as ``1``/``0``
    so SQLite stores them as it does for ORM-inserted rows.
    """
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def _add_column_sql(table: str, column, dialect) -> str:
    """Build a SQLite-safe ``ALTER TABLE ... ADD COLUMN`` statement from a model column.

    A ``NOT NULL`` column MUST carry a constant default (SQLite requires one to back-fill the
    existing rows); a nullable column without a constant default is added without a ``DEFAULT``
    so existing rows receive ``NULL``.
    """
    coltype = column.type.compile(dialect=dialect)
    parts = [f'ALTER TABLE {table} ADD COLUMN "{column.name}" {coltype}']
    default_sql = _scalar_default_sql(column)
    if not column.nullable:
        if default_sql is None:
            raise ValueError(
                f"non-null column {table}.{column.name} has no constant default to back-fill"
            )
        parts.append(f"NOT NULL DEFAULT {default_sql}")
    elif default_sql is not None:
        parts.append(f"DEFAULT {default_sql}")
    return " ".join(parts)


def ensure_tables(conn: Connection, names: list[str]) -> None:
    """Create each named current-metadata table (with its indexes) if it does not yet exist."""
    for name in names:
        Base.metadata.tables[name].create(bind=conn, checkfirst=True)


def ensure_indexes(conn: Connection, names: list[str]) -> None:
    """Create any current-metadata index on the named tables that is absent (idempotent).

    Used to add indexes to *pre-existing* tables (a freshly created table already has its
    indexes). ``checkfirst`` skips indexes that already exist. An index whose column(s) have
    not yet been added (they belong to a later migration) is skipped here and created by that
    later migration once the column exists — so ordering across staged column additions is
    always safe.
    """
    for name in names:
        existing_cols = _existing_columns(conn, name)
        if not existing_cols:
            continue
        for index in Base.metadata.tables[name].indexes:
            if not all(col.name in existing_cols for col in index.columns):
                continue
            index.create(bind=conn, checkfirst=True)


def add_missing_columns(conn: Connection, table: str, columns: list[str]) -> None:
    """Add each named model column to ``table`` if absent. Inspects the real schema first."""
    existing = _existing_columns(conn, table)
    model_table = Base.metadata.tables[table]
    for column_name in columns:
        if column_name in existing:
            continue
        column = model_table.columns[column_name]
        conn.execute(text(_add_column_sql(table, column, conn.dialect)))


# -- ordered migrations ----------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """One ordered, idempotent, additive schema change."""

    id: str
    description: str
    apply: Callable[[Connection], None]


def _m_0001_core_baseline(conn: Connection) -> None:
    # Ensure the Stage 1–12 core tables exist (fresh or very old database). On a fresh
    # database these are created with the full current column set, which makes every later
    # column migration a no-op; on an old database existing tables are left untouched and the
    # later migrations add their newer columns.
    ensure_tables(
        conn,
        [
            "missions",
            "events",
            "mission_steps",
            "mission_execution_runs",
            "coding_tasks",
            "coding_task_results",
            "mcp_tool_calls",
            "gnuradio_context",
            "external_agents",
            "external_agent_messages",
        ],
    )


def _m_0002_transport_peer_columns(conn: Connection) -> None:
    # Stage 13A: an external-agent row doubles as a transport PEER. These identity/trust
    # columns are additive; nullable identity fields stay nullable, and a peer is `untrusted`
    # with role `unknown` until an operator explicitly trusts its public key.
    add_missing_columns(
        conn,
        "external_agents",
        [
            "role",
            "expected_node_id",
            "public_key",
            "fingerprint",
            "trust_state",
            "enabled",
            "tls_verify",
            "last_success_at",
            "last_failure_at",
            "consecutive_failures",
            "last_manifest_json",
            "capability_snapshot_json",
            "capability_snapshot_at",
            "last_error",
        ],
    )
    ensure_indexes(conn, ["external_agents"])  # ix_external_agents_fingerprint


def _m_0003_transport_inbox_outbox(conn: Connection) -> None:
    # Stage 13A: durable signed-transport inbox/outbox tables and their indexes.
    ensure_tables(conn, ["agent_outbox_messages", "agent_inbox_messages"])
    ensure_indexes(conn, ["agent_outbox_messages", "agent_inbox_messages"])


def _m_0004_rf_backends(conn: Connection) -> None:
    # Stage 13A.5: multi-RF-backend tables, the per-call backend audit columns on
    # mcp_tool_calls (existing rows default to the legacy GNU Radio backend), and indexes.
    ensure_tables(conn, ["rf_workspace_context", "rf_artifacts", "rf_benchmark_runs"])
    add_missing_columns(
        conn,
        "mcp_tool_calls",
        [
            "backend_id",
            "backend_kind",
            "backend_version",
            "backend_source_revision",
            "result_summary_type",
        ],
    )
    ensure_indexes(
        conn,
        ["mcp_tool_calls", "rf_workspace_context", "rf_artifacts", "rf_benchmark_runs"],
    )


def _m_0005_comms_peer_authorization(conn: Connection) -> None:
    # Stage 13B: application authorization, SEPARATE from public-key trust. Conservative
    # defaults — a trusted peer may NOT open inbound coordinator requests or demand a response
    # until explicitly granted; reply acceptance / receiving messages follow the model design.
    add_missing_columns(
        conn,
        "external_agents",
        [
            "may_send_requests",
            "may_send_replies",
            "may_receive_messages",
            "may_request_response",
            "max_inbound_request_bytes",
            "max_concurrent_inbound_missions",
        ],
    )


def _m_0006_comms_application_columns(conn: Connection) -> None:
    # Stage 13B: denormalized application-correlation columns on the transport inbox/outbox
    # (the application payload itself rides inside envelope_json) and their indexes.
    add_missing_columns(
        conn,
        "agent_outbox_messages",
        ["application_type", "application_request_id", "reply_to_request_id", "expects_reply"],
    )
    add_missing_columns(
        conn,
        "agent_inbox_messages",
        ["application_type", "application_request_id", "reply_to_request_id", "application_state"],
    )
    ensure_indexes(conn, ["agent_outbox_messages", "agent_inbox_messages"])


def _m_0007_reply_waits_and_inbound_requests(conn: Connection) -> None:
    # Stage 13B: durable reply-wait and inbound-request tables, with their correlation indexes.
    ensure_tables(conn, ["mission_reply_waits", "inbound_requests"])
    ensure_indexes(conn, ["mission_reply_waits", "inbound_requests"])


def _m_0009_artifact_columns(conn: Connection) -> None:
    # Stage 13D.2: per-peer artifact authorization (separate from trust + message authorization)
    # and transferable RF-artifact identity/evidence columns. All additive and conservative.
    add_missing_columns(
        conn,
        "external_agents",
        [
            "may_offer_artifacts", "may_request_artifacts", "may_receive_artifacts",
            "max_artifact_bytes", "max_concurrent_transfers", "allowed_artifact_kinds_json",
        ],
    )
    add_missing_columns(
        conn,
        "rf_artifacts",
        [
            "origin_node_id", "origin_artifact_id", "backend_version", "backend_source_revision",
            "conversation_id", "request_id", "display_name", "digest", "object_ref",
            "availability_state", "pinned", "retention_state",
        ],
    )
    ensure_indexes(conn, ["rf_artifacts"])


def _m_0010_artifact_transfer_tables(conn: Connection) -> None:
    # Stage 13D.2: durable transfer + remote-offer tables (bytes live on disk, never here).
    ensure_tables(
        conn,
        ["artifact_transfers", "artifact_transfer_attempts", "remote_artifact_references"],
    )
    ensure_indexes(
        conn,
        ["artifact_transfers", "artifact_transfer_attempts", "remote_artifact_references"],
    )


def _m_0011_artifact_attempt_offset(conn: Connection) -> None:
    # Stage 13D.2 restart proof: the byte offset each transfer attempt started from (0 for a
    # fresh attempt, > 0 when an attempt resumes after a process restart). Additive + idempotent.
    add_missing_columns(conn, "artifact_transfer_attempts", ["start_offset"])


def _m_0012_mission_status_permissions(conn: Connection) -> None:
    # Stage 13D.3: per-peer mission-status authorization (separate from trust + message +
    # artifact permissions). All additive and conservative (default false).
    add_missing_columns(
        conn,
        "external_agents",
        [
            "may_publish_mission_status", "may_query_mission_status",
            "may_receive_mission_status", "max_active_remote_snapshots",
        ],
    )


def _m_0013_mission_status_tables(conn: Connection) -> None:
    # Stage 13D.3: local publication bookkeeping + remote snapshot + append-only status events.
    ensure_tables(
        conn,
        [
            "local_mission_status_publications",
            "remote_mission_snapshots",
            "remote_mission_status_events",
        ],
    )
    ensure_indexes(
        conn,
        [
            "local_mission_status_publications",
            "remote_mission_snapshots",
            "remote_mission_status_events",
        ],
    )


def _m_0014_hardware_tables(conn: Connection) -> None:
    # Stage 14B: managed SDR hardware inventory, capabilities, health, bindings, leases. Tables
    # + indexes only (bytes/secrets never stored). Additive and idempotent.
    ensure_tables(
        conn,
        [
            "sdr_devices",
            "sdr_device_capabilities",
            "sdr_device_health_events",
            "sdr_device_backend_bindings",
            "sdr_device_leases",
            "sdr_device_lease_events",
        ],
    )
    ensure_indexes(
        conn,
        [
            "sdr_devices",
            "sdr_device_capabilities",
            "sdr_device_health_events",
            "sdr_device_backend_bindings",
            "sdr_device_leases",
            "sdr_device_lease_events",
        ],
    )


def _m_0015_mission_run_rf_device_counter(conn: Connection) -> None:
    # Stage 14B: per-run counter for model-selected rf_device lease actions (one MissionStep
    # each; renewal/polling never increment it). Additive NOT NULL DEFAULT 0.
    add_missing_columns(conn, "mission_execution_runs", ["rf_device_action_count"])


def _m_0016_hardware_qualification_tables(conn: Connection) -> None:
    # Stage 14C.1: real-hardware qualification runs/checks + physical RF measurement provenance.
    # Tables + indexes only; large sample bytes live in the artifact store, never here.
    ensure_tables(
        conn,
        [
            "hardware_qualification_runs",
            "hardware_qualification_checks",
            "hardware_rf_measurements",
        ],
    )
    ensure_indexes(
        conn,
        [
            "hardware_qualification_runs",
            "hardware_qualification_checks",
            "hardware_rf_measurements",
        ],
    )


_FIELD_TABLES = [
    "field_campaigns", "field_nodes", "field_device_assignments", "field_runs", "field_checks",
    "field_measurements", "field_fault_injections", "field_recovery_results",
    "soak_runs", "soak_samples",
]


def _m_0017_field_validation_tables(conn: Connection) -> None:
    # Stage 14C.2: multi-node field-validation campaign / soak / fault-injection tables.
    ensure_tables(conn, _FIELD_TABLES)
    ensure_indexes(conn, _FIELD_TABLES)


_INTEROP_TABLES = [
    "interop_agents", "interop_credentials", "interop_endpoints", "interop_subscriptions",
    "interop_sessions", "interop_messages", "interop_deliveries", "interop_receipts",
    "interop_nonces", "interop_submissions", "interop_audit",
]


def _m_0018_interop_tables(conn: Connection) -> None:
    # Stage 14D: external-agent interoperability — identity, credentials, endpoints,
    # subscriptions, sessions, durable message outbox, deliveries, receipts, nonce cache,
    # idempotent submissions, and a sanitized audit log.
    ensure_tables(conn, _INTEROP_TABLES)
    ensure_indexes(conn, _INTEROP_TABLES)


_DATA_PLATFORM_TABLES = [
    "data_consent_profiles", "data_consent_grants", "data_consent_revisions",
    "data_consent_audit", "data_category_policies", "data_export_records",
    "data_export_batches", "data_export_destinations", "data_delivery_attempts",
    "data_deletion_requests", "data_datasets", "data_dataset_versions", "data_dataset_members",
]


def _m_0019_data_platform_tables(conn: Connection) -> None:
    # Stage 14E: privacy-preserving telemetry / consent / export-outbox / batch / destination /
    # deletion / dataset-lineage tables (node side). Nothing here stores secrets.
    ensure_tables(conn, _DATA_PLATFORM_TABLES)
    ensure_indexes(conn, _DATA_PLATFORM_TABLES)


_MESH_TABLES = ["meshes", "mesh_members"]


def _m_0020_mesh_tables(conn: Connection) -> None:
    # beta.3: first-class mesh model (node side) — mesh authority + membership for tenant-isolated
    # secure mesh. Never stores a private key.
    ensure_tables(conn, _MESH_TABLES)
    ensure_indexes(conn, _MESH_TABLES)


def _m_0008_align_current_metadata(conn: Connection) -> None:
    # Safety net: create any current-metadata table or index still absent (covers a brand-new
    # database in one shot and any future additive *table*). This NEVER alters an existing
    # table — column additions are owned by the explicit migrations above.
    Base.metadata.create_all(bind=conn, checkfirst=True)


MIGRATIONS: list[Migration] = [
    Migration("0001_core_baseline", "Ensure Stage 1–12 core tables exist", _m_0001_core_baseline),
    Migration(
        "0002_transport_peer_columns",
        "Stage 13A peer/transport identity + trust columns",
        _m_0002_transport_peer_columns,
    ),
    Migration(
        "0003_transport_inbox_outbox",
        "Stage 13A signed transport inbox/outbox tables + indexes",
        _m_0003_transport_inbox_outbox,
    ),
    Migration(
        "0004_rf_backends",
        "Stage 13A.5 RF tables + mcp_tool_calls backend columns + indexes",
        _m_0004_rf_backends,
    ),
    Migration(
        "0005_comms_peer_authorization",
        "Stage 13B peer application-authorization columns",
        _m_0005_comms_peer_authorization,
    ),
    Migration(
        "0006_comms_application_columns",
        "Stage 13B inbox/outbox application-correlation columns + indexes",
        _m_0006_comms_application_columns,
    ),
    Migration(
        "0007_reply_waits_and_inbound_requests",
        "Stage 13B reply-wait + inbound-request tables + indexes",
        _m_0007_reply_waits_and_inbound_requests,
    ),
    Migration(
        "0009_artifact_columns",
        "Stage 13D.2 peer artifact-authorization + RF-artifact identity columns + indexes",
        _m_0009_artifact_columns,
    ),
    Migration(
        "0010_artifact_transfer_tables",
        "Stage 13D.2 artifact-transfer + remote-offer tables + indexes",
        _m_0010_artifact_transfer_tables,
    ),
    Migration(
        "0011_artifact_attempt_offset",
        "Stage 13D.2 transfer-attempt start_offset column",
        _m_0011_artifact_attempt_offset,
    ),
    Migration(
        "0012_mission_status_permissions",
        "Stage 13D.3 per-peer mission-status authorization columns",
        _m_0012_mission_status_permissions,
    ),
    Migration(
        "0013_mission_status_tables",
        "Stage 13D.3 remote-snapshot + status-event + local-publication tables + indexes",
        _m_0013_mission_status_tables,
    ),
    Migration(
        "0014_hardware_tables",
        "Stage 14B managed SDR hardware inventory/capability/health/binding/lease tables",
        _m_0014_hardware_tables,
    ),
    Migration(
        "0015_mission_run_rf_device_counter",
        "Stage 14B mission_execution_runs.rf_device_action_count column",
        _m_0015_mission_run_rf_device_counter,
    ),
    Migration(
        "0016_hardware_qualification_tables",
        "Stage 14C.1 hardware qualification runs/checks + RF measurement provenance tables",
        _m_0016_hardware_qualification_tables,
    ),
    Migration(
        "0017_field_validation_tables",
        "Stage 14C.2 field campaign / soak / fault-injection tables",
        _m_0017_field_validation_tables,
    ),
    Migration(
        "0018_interop_tables",
        "Stage 14D external-agent interoperability identity/delivery/subscription tables",
        _m_0018_interop_tables,
    ),
    Migration(
        "0019_data_platform_tables",
        "Stage 14E privacy/consent/export-outbox/batch/dataset-lineage tables",
        _m_0019_data_platform_tables,
    ),
    Migration(
        "0020_mesh_tables",
        "beta.3 first-class mesh model (mesh authority + membership) node-side tables",
        _m_0020_mesh_tables,
    ),
    Migration(
        "0008_align_current_metadata",
        "Create any remaining current-metadata table/index (safety net)",
        _m_0008_align_current_metadata,
    ),
]


# -- history table + runner ------------------------------------------------------------------


class MigrationError(RuntimeError):
    """A migration failed. Carries only the migration id and error *type* — never SQL,
    parameters, row values, keys, signatures, prompts, or environment."""

    def __init__(self, migration_id: str, error_type: str) -> None:
        self.migration_id = migration_id
        self.error_type = error_type
        super().__init__(f"migration '{migration_id}' failed ({error_type})")


@dataclass
class MigrationReport:
    """Outcome of a :func:`run_migrations` call."""

    dialect: str
    applied_now: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)


def _ensure_history_table(conn: Connection) -> None:
    conn.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} ("
            "migration_id TEXT PRIMARY KEY NOT NULL, "
            "applied_at TEXT NOT NULL)"
        )
    )


def _applied_ids(conn: Connection) -> set[str]:
    if not _table_exists(conn, SCHEMA_MIGRATIONS_TABLE):
        return set()
    rows = conn.execute(text(f"SELECT migration_id FROM {SCHEMA_MIGRATIONS_TABLE}"))
    return {row[0] for row in rows}


def run_migrations(engine: Engine) -> MigrationReport:
    """Apply every pending migration in order, exactly once, before any repository query.

    Each pending migration runs in its own transaction together with the insert that records
    it, so the history row is written only after the DDL commits. A failure rolls the whole
    migration back (recording nothing) and raises a sanitized :class:`MigrationError`.
    """
    with engine.begin() as conn:
        _ensure_history_table(conn)
        applied = _applied_ids(conn)

    report = MigrationReport(dialect=engine.dialect.name)
    for migration in MIGRATIONS:
        if migration.id in applied:
            report.already_applied.append(migration.id)
            continue
        try:
            with engine.begin() as conn:
                migration.apply(conn)
                conn.execute(
                    text(
                        f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (migration_id, applied_at) "
                        "VALUES (:migration_id, :applied_at)"
                    ),
                    {"migration_id": migration.id, "applied_at": utcnow().isoformat()},
                )
        except Exception as exc:  # noqa: BLE001 — re-raised sanitized below
            raise MigrationError(migration.id, type(exc).__name__) from None
        report.applied_now.append(migration.id)
    return report


# -- validation + status ---------------------------------------------------------------------


@dataclass
class ValidationResult:
    """The result of comparing the live schema against the current ``Base.metadata``.

    Reports objects the current models require but the database lacks. Extra (unknown)
    columns or tables are deliberately NOT reported — they are preserved, never removed.
    """

    ok: bool
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    pending_migrations: list[str] = field(default_factory=list)


def validate_schema(engine: Engine) -> ValidationResult:
    """Validate that every current model table, column, and index exists and no migration
    is pending. Does not flag or remove unknown extra columns/tables."""
    insp = inspect(engine)
    db_tables = set(insp.get_table_names())
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    missing_indexes: list[str] = []

    for table_name, table in Base.metadata.tables.items():
        if table_name not in db_tables:
            missing_tables.append(table_name)
            continue
        db_columns = {col["name"] for col in insp.get_columns(table_name)}
        for column in table.columns:
            if column.name not in db_columns:
                missing_columns.append(f"{table_name}.{column.name}")
        db_indexes = {idx["name"] for idx in insp.get_indexes(table_name)}
        for index in table.indexes:
            if index.name not in db_indexes:
                missing_indexes.append(f"{table_name}.{index.name}")

    with engine.begin() as conn:
        applied = _applied_ids(conn)
    pending = [migration.id for migration in MIGRATIONS if migration.id not in applied]

    ok = not (missing_tables or missing_columns or missing_indexes or pending)
    return ValidationResult(
        ok=ok,
        missing_tables=sorted(missing_tables),
        missing_columns=sorted(missing_columns),
        missing_indexes=sorted(missing_indexes),
        pending_migrations=pending,
    )


@dataclass
class MigrationStatus:
    """A point-in-time view of migration history + schema validity for the ``database`` CLI."""

    dialect: str
    applied: list[tuple[str, str]]  # (migration_id, applied_at)
    pending: list[str]
    validation: ValidationResult


def migration_status(engine: Engine) -> MigrationStatus:
    """Return applied history, pending ids, and schema validation for an existing database."""
    with engine.begin() as conn:
        _ensure_history_table(conn)
        if _table_exists(conn, SCHEMA_MIGRATIONS_TABLE):
            rows = conn.execute(
                text(
                    f"SELECT migration_id, applied_at FROM {SCHEMA_MIGRATIONS_TABLE} "
                    "ORDER BY migration_id"
                )
            ).all()
        else:  # pragma: no cover - history table is ensured above
            rows = []
    applied = [(row[0], row[1]) for row in rows]
    applied_ids = {migration_id for migration_id, _ in applied}
    pending = [migration.id for migration in MIGRATIONS if migration.id not in applied_ids]
    return MigrationStatus(
        dialect=engine.dialect.name,
        applied=applied,
        pending=pending,
        validation=validate_schema(engine),
    )
