"""Database engine and session-factory bootstrap for the node state store."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_db_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine for ``database_url``.

    ``check_same_thread`` is disabled for SQLite so a single engine can be shared
    across the API event loop and the TestClient's worker thread.
    """
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to ``engine``.

    ``expire_on_commit`` is disabled so attributes remain readable immediately after
    commit while building API responses.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """Bring the schema to the current models via ordered, additive migrations.

    Historically this called ``Base.metadata.create_all()``, which creates missing *tables*
    but never adds *columns* to an existing SQLite table — so a database created by an older
    schema kept failing with ``no such column``. The migration framework
    (:mod:`aithernet.state.migrations`) runs every pending additive migration exactly once,
    non-destructively, BEFORE any repository queries the current ORM models. It is idempotent,
    so a fresh database (already at the current schema) and a fully migrated one are no-ops.
    """
    from aithernet.state.migrations import run_migrations

    run_migrations(engine)
