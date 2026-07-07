"""Persistence for the GNU Radio workspace context (one current record per node)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aithernet.state.models import GNURadioContextRecord, utcnow


class GNURadioContextRepository:
    """Get/upsert the single current GNU Radio context row for a node."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, node_id: str) -> GNURadioContextRecord | None:
        stmt = select(GNURadioContextRecord).where(GNURadioContextRecord.node_id == node_id)
        return self.session.scalars(stmt).first()

    def get_or_create(self, node_id: str) -> GNURadioContextRecord:
        record = self.get(node_id)
        if record is None:
            record = GNURadioContextRecord(node_id=node_id)
            self.session.add(record)
            self.session.flush()
        return record

    def update(
        self, node_id: str, *, fields: dict, bump_version: bool = True
    ) -> GNURadioContextRecord:
        """Apply ``fields`` to the node's context record, bumping the version."""
        record = self.get_or_create(node_id)
        for key, value in fields.items():
            setattr(record, key, value)
        if bump_version:
            record.context_version = (record.context_version or 0) + 1
        record.last_changed_at = utcnow()
        self.session.flush()
        return record
