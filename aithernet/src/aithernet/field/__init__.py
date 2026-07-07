"""Multi-node physical RF field operation, fault recovery, and soak validation (Stage 14C.2).

Operator-invoked, deterministic validation of the complete node under repeated-operation, failure,
and long-duration conditions. Reuses the existing hardware lease/capture, transport, and artifact
subsystems without redesigning them. No fake result is ever classified as physical evidence.
"""

from __future__ import annotations

from aithernet.field.service import FieldService

__all__ = ["FieldService"]
