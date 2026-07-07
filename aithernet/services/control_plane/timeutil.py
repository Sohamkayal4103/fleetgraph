"""Datetime coercion shared across the control plane.

SQLite (tests) returns timezone-naive datetimes from ``DateTime(timezone=True)`` columns while
PostgreSQL (production) returns aware ones. Comparisons against an aware clock must therefore
coerce stored values to aware UTC first, so the same code is correct on both dialects.
"""

from __future__ import annotations

from datetime import UTC, datetime


def ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
