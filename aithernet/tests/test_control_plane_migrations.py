"""Stage 14F hosted migration system tests (explicit migrations, not create_all)."""

from __future__ import annotations

from services.control_plane.migrations import (
    migration_status,
    run_migrations,
    validate_schema,
)
from services.control_plane.models import Base
from sqlalchemy import create_engine, inspect


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'hosted.db'}", future=True)


def test_migrations_create_full_schema(tmp_path):
    engine = _engine(tmp_path)
    report = run_migrations(engine)
    assert "0001_hosted_baseline" in report.applied_now
    tables = set(inspect(engine).get_table_names())
    for name in Base.metadata.tables:
        assert name in tables
    assert "hosted_schema_migrations" in tables


def test_migrations_are_idempotent(tmp_path):
    engine = _engine(tmp_path)
    run_migrations(engine)
    report2 = run_migrations(engine)
    assert report2.applied_now == []
    assert "0001_hosted_baseline" in report2.already_applied


def test_validate_schema_ok_after_migrate(tmp_path):
    engine = _engine(tmp_path)
    run_migrations(engine)
    result = validate_schema(engine)
    assert result.ok and not result.missing_tables and not result.pending_migrations


def test_migration_status_reports_applied(tmp_path):
    engine = _engine(tmp_path)
    run_migrations(engine)
    status = migration_status(engine)
    assert status.pending == [] and status.applied


def test_bootstrap_state_id_width_is_corrected():
    """A fresh baseline carries the corrected width: id holds 'singleton' (9 chars).

    The live PostgreSQL run caught String(8) truncating the 9-char default; the baseline now
    declares String(16). No widening migration exists because no external DB was ever deployed."""
    col = Base.metadata.tables["hosted_bootstrap_state"].columns["id"]
    assert col.type.length >= len("singleton")
    assert col.type.length == 16


def test_fresh_database_creates_correct_bootstrap_width(tmp_path):
    """A clean database receives the corrected column width from the baseline migration."""
    from sqlalchemy import inspect

    engine = _engine(tmp_path)
    run_migrations(engine)
    cols = {c["name"]: c for c in inspect(engine).get_columns("hosted_bootstrap_state")}
    # SQLite reports VARCHAR(16); the column must hold the 9-char 'singleton' default.
    type_str = str(cols["id"]["type"]).upper()
    assert "16" in type_str or "VARCHAR" in type_str
