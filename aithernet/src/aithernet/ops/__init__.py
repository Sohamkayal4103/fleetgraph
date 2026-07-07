"""Stage 14A production-operations layer.

Deterministic, repository-independent node lifecycle: a production directory layout, idempotent
provisioning, configuration preflight, readiness/liveness, consistent SQLite backup/restore, a
safe upgrade/rollback workflow, and a sanitized diagnostics bundle. None of this redesigns the
mission engine, coordinator, transport, RF backends, or distributed protocols — it makes the
completed node safely installable, runnable, upgradeable, recoverable, observable and supportable
outside the development checkout.
"""

from aithernet.ops.paths import NodePaths

__all__ = ["NodePaths"]
