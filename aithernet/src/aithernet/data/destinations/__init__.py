"""Export-destination abstraction (Stage 14E, Part 12).

The core selection/consent/queue logic depends only on this interface, never on Google Drive
or any cloud vendor. Each destination validates its config, reports readiness, uploads a sealed
bundle idempotently, parses + verifies a receipt, supports deletion where applicable, reports
health, and sanitizes everything it returns (no secrets, tokens, keys, or raw paths).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UploadResult:
    """The sanitized outcome of one batch upload attempt."""

    ok: bool
    receipt: dict = field(default_factory=dict)
    failure_category: str | None = None
    detail: str = ""


@dataclass
class DeletionResult:
    ok: bool
    state: str = "unknown"      # deleted | not_found | unknown | unsupported
    receipt: dict = field(default_factory=dict)
    detail: str = ""


@dataclass
class Readiness:
    ready: bool
    state: str                  # ready | degraded | unconfigured | disabled
    detail: str = ""


class ExportDestination:
    """Base class for export destinations. Concrete subclasses override the lifecycle hooks."""

    kind: str = "abstract"

    def validate(self) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for the destination's configuration."""
        return True, "ok"

    def readiness(self) -> Readiness:
        return Readiness(ready=True, state="ready")

    async def upload(self, *, batch_id: str, idempotency_key: str, bundle: bytes,
                     manifest: dict) -> UploadResult:
        raise NotImplementedError

    async def delete(self, *, remote_ref: str) -> DeletionResult:
        return DeletionResult(ok=False, state="unsupported", detail="deletion not supported")

    def health(self) -> dict:
        r = self.readiness()
        return {"kind": self.kind, "ready": r.ready, "state": r.state, "detail": r.detail}
