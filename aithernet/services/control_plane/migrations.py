"""Explicit, ordered, additive migrations for the hosted control-plane database (Stage 14F §6).

The control plane does NOT use ``Base.metadata.create_all()`` as its production migration
strategy. This module owns an ordered set of idempotent, additive migrations that bring any
earlier hosted database up to the current ``Base.metadata`` exactly once. The design mirrors the
node's :mod:`aithernet.state.migrations`: additive only, idempotent, ordered + recorded in
``hosted_schema_migrations``, dialect-aware (SQLite for tests, PostgreSQL in production). A
migration that raises records nothing and is retried in full on the next run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, inspect, text

from services.control_plane.models import Base, utcnow

SCHEMA_MIGRATIONS_TABLE = "hosted_schema_migrations"


class MigrationError(RuntimeError):
    """A hosted migration failed (sanitized — migration id + error *type* only)."""

    def __init__(self, migration_id: str, error_type: str) -> None:
        super().__init__(f"hosted_migration_failed:{migration_id}:{error_type}")
        self.migration_id = migration_id
        self.error_type = error_type


def _table_exists(conn: Connection, name: str) -> bool:
    return inspect(conn).has_table(name)


def _existing_columns(conn: Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {col["name"] for col in inspect(conn).get_columns(table)}


def ensure_tables(conn: Connection, names: list[str]) -> None:
    for name in names:
        Base.metadata.tables[name].create(bind=conn, checkfirst=True)


def ensure_indexes(conn: Connection, names: list[str]) -> None:
    for name in names:
        existing_cols = _existing_columns(conn, name)
        if not existing_cols:
            continue
        for index in Base.metadata.tables[name].indexes:
            if not all(col.name in existing_cols for col in index.columns):
                continue
            index.create(bind=conn, checkfirst=True)


def ensure_columns(conn: Connection, table: str, columns: list[str]) -> None:
    """Additively add any missing columns to an EXISTING table (idempotent, dialect-agnostic).

    Uses ``ALTER TABLE ... ADD COLUMN`` — supported by both SQLite (tests) and PostgreSQL
    (production) for nullable columns. Columns already present are left untouched. Only columns
    named here (and defined on ``Base.metadata``) are added; nothing is ever dropped or altered."""
    if not _table_exists(conn, table):
        return
    existing = _existing_columns(conn, table)
    tbl = Base.metadata.tables[table]
    dialect = conn.dialect
    for col_name in columns:
        if col_name in existing:
            continue
        column = tbl.columns[col_name]
        col_type = column.type.compile(dialect=dialect)
        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}'))


@dataclass(frozen=True)
class Migration:
    id: str
    description: str
    apply: Callable[[Connection], None]


def _m_0001_hosted_baseline(conn: Connection) -> None:
    # Fresh database: create every current control-plane table with its full column set + indexes.
    ensure_tables(conn, list(Base.metadata.tables.keys()))
    ensure_indexes(conn, list(Base.metadata.tables.keys()))


def _m_0002_public_intake(conn: Connection) -> None:
    # Public early-access requests + mailing-list subscribers (added after the baseline).
    names = ["hosted_early_access_requests", "hosted_mailing_subscribers"]
    ensure_tables(conn, names)
    ensure_indexes(conn, names)


def _m_0003_mesh_tables(conn: Connection) -> None:
    # beta.3: hosted mesh + membership tables (synchronize the node-side first-class mesh model).
    names = ["hosted_meshes", "hosted_mesh_members"]
    ensure_tables(conn, names)
    ensure_indexes(conn, names)


def _m_0004_research_tables(conn: Connection) -> None:
    # beta.9: hosted research / owner-archive pipeline (enrolled nodes upload sanitized research
    # packages into the owner/admin archive; owner Drive connection is server-side only).
    names = [
        "research_upload_capabilities",
        "research_packages",
        "research_package_artifacts",
        "research_quarantine",
        "research_archive_jobs",
        "research_archive_ledger",
        "research_collection_policies",
        "research_owner_archive_connections",
    ]
    ensure_tables(conn, names)
    ensure_indexes(conn, names)


def _m_0005_owner_drive_oauth(conn: Connection) -> None:
    # Admin owner-Drive OAuth connect flow: the single-use OAuth state table, plus additive
    # columns on the existing owner-archive connection row (server/portal-only; no client change).
    ensure_tables(conn, ["research_oauth_states"])
    ensure_indexes(conn, ["research_oauth_states"])
    ensure_columns(conn, "research_owner_archive_connections",
                   ["root_folder_name", "account_email", "scope", "error_category",
                    "connected_via"])


MIGRATIONS: list[Migration] = [
    Migration("0001_hosted_baseline", "Create hosted control-plane baseline schema",
              _m_0001_hosted_baseline),
    Migration("0002_public_intake", "Add early-access request + mailing-list tables",
              _m_0002_public_intake),
    Migration("0003_mesh_tables", "Add hosted mesh + mesh-member tables (beta.3)",
              _m_0003_mesh_tables),
    Migration("0004_research_tables",
              "Add hosted research / owner-archive pipeline tables (beta.9)",
              _m_0004_research_tables),
    Migration("0005_owner_drive_oauth",
              "Add admin owner-Drive OAuth state table + connection columns",
              _m_0005_owner_drive_oauth),
]


@dataclass
class MigrationReport:
    dialect: str
    already_applied: list[str] = field(default_factory=list)
    applied_now: list[str] = field(default_factory=list)


def _ensure_history_table(conn: Connection) -> None:
    conn.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} ("
            "migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
    )


def _applied_ids(conn: Connection) -> set[str]:
    if not _table_exists(conn, SCHEMA_MIGRATIONS_TABLE):
        return set()
    rows = conn.execute(text(f"SELECT migration_id FROM {SCHEMA_MIGRATIONS_TABLE}"))
    return {row[0] for row in rows}


def run_migrations(engine: Engine) -> MigrationReport:
    """Apply every pending hosted migration in order, exactly once, before any query."""
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
        except Exception as exc:  # noqa: BLE001 — re-raised sanitized
            raise MigrationError(migration.id, type(exc).__name__) from None
        report.applied_now.append(migration.id)
    return report


@dataclass
class ValidationResult:
    ok: bool
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    pending_migrations: list[str] = field(default_factory=list)


def validate_schema(engine: Engine) -> ValidationResult:
    insp = inspect(engine)
    db_tables = set(insp.get_table_names())
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in db_tables:
            missing_tables.append(table_name)
            continue
        db_columns = {col["name"] for col in insp.get_columns(table_name)}
        for column in table.columns:
            if column.name not in db_columns:
                missing_columns.append(f"{table_name}.{column.name}")
    with engine.begin() as conn:
        applied = _applied_ids(conn)
    pending = [m.id for m in MIGRATIONS if m.id not in applied]
    ok = not (missing_tables or missing_columns or pending)
    return ValidationResult(ok, sorted(missing_tables), sorted(missing_columns), pending)


@dataclass
class MigrationStatus:
    dialect: str
    applied: list[tuple[str, str]]
    pending: list[str]
    validation: ValidationResult


def migration_status(engine: Engine) -> MigrationStatus:
    with engine.begin() as conn:
        _ensure_history_table(conn)
        rows = conn.execute(
            text(
                f"SELECT migration_id, applied_at FROM {SCHEMA_MIGRATIONS_TABLE} "
                "ORDER BY migration_id"
            )
        ).all()
    applied = [(row[0], row[1]) for row in rows]
    applied_ids = {mid for mid, _ in applied}
    pending = [m.id for m in MIGRATIONS if m.id not in applied_ids]
    return MigrationStatus(engine.dialect.name, applied, pending, validate_schema(engine))
