"""Logical backup + restore for the hosted control plane (Stage 14F §38, §46).

A dialect-independent JSON dump of all hosted tables (SQLite for tests, PostgreSQL in production).
The dump contains hashed/digested values exactly as stored — it never contains plaintext passwords
or raw tokens, because those are never persisted in the first place. Restore loads into an already
*migrated* database, so schema creation stays owned by the migration system.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, delete, insert, select

from services.control_plane.models import Base


def _serialize(value):
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    return value


def _deserialize(value):
    if isinstance(value, dict) and "__dt__" in value:
        return datetime.fromisoformat(value["__dt__"])
    return value


def export_database(engine: Engine) -> dict:
    data: dict[str, list[dict]] = {}
    with engine.begin() as conn:
        for name, table in Base.metadata.tables.items():
            rows = conn.execute(select(table)).mappings().all()
            data[name] = [{k: _serialize(v) for k, v in row.items()} for row in rows]
    return {"version": 1, "tables": data}


def backup_to_file(engine: Engine, output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(export_database(engine), indent=2, sort_keys=True))
    tmp.replace(path)
    return path


def restore_from_file(engine: Engine, input_path: str | Path) -> dict:
    payload = json.loads(Path(input_path).read_text())
    tables = payload.get("tables", {})
    restored: dict[str, int] = {}
    # Insert in metadata (dependency-friendly) order; clear existing rows first.
    ordered = list(Base.metadata.sorted_tables)
    with engine.begin() as conn:
        for table in reversed(ordered):
            conn.execute(delete(table))
        for table in ordered:
            rows = tables.get(table.name, [])
            if not rows:
                continue
            conn.execute(insert(table),
                         [{k: _deserialize(v) for k, v in row.items()} for row in rows])
            restored[table.name] = len(rows)
    return {"restored": restored}
