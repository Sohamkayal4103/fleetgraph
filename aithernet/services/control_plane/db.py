"""Hosted control-plane database engine + session lifecycle (Stage 14F §6, §36).

Tests use temporary SQLite; production uses PostgreSQL with bounded pooling — the same models and
migrations run on both with no architectural change. Migrations (not ``create_all``) own schema
creation; :func:`init_database` runs them before the first query.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from services.control_plane.config import HostedConfig
from services.control_plane.migrations import run_migrations


def create_hosted_engine(config: HostedConfig) -> Engine:
    if config.is_sqlite:
        return create_engine(config.database_url, future=True)
    return create_engine(
        config.database_url,
        future=True,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_pre_ping=True,
    )


def init_database(engine: Engine) -> None:
    """Apply pending hosted migrations (explicit migration system; never ``create_all``)."""
    run_migrations(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
