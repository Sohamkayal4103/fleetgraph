"""Air-gapped bundle destination (Stage 14E, Part 22).

A first-class (not fallback) destination: approved, encrypted bundles are written to local /
removable media with a manifest, for later manual transfer + receipt import. No network is
required and no cloud credentials are loaded; readiness stays healthy. Reuses the path-safe
local archive writer, but its receipt is explicitly "written, pending import" so a delivery is
never reported as remotely confirmed until a receipt is imported.
"""

from __future__ import annotations

from aithernet.data.destinations import Readiness, UploadResult
from aithernet.data.destinations.local import LocalArchiveDestination


class AirGappedBundleDestination(LocalArchiveDestination):
    kind = "air_gapped"

    def readiness(self) -> Readiness:
        base = super().readiness()
        if not base.ready:
            return base
        return Readiness(ready=True, state="ready", detail="air_gapped")

    async def upload(self, *, batch_id: str, idempotency_key: str, bundle: bytes,
                     manifest: dict) -> UploadResult:
        result = await super().upload(
            batch_id=batch_id, idempotency_key=idempotency_key, bundle=bundle, manifest=manifest,
        )
        if result.ok:
            # Written locally, but NOT remotely confirmed — an operator imports the receipt later.
            result.receipt["destination_kind"] = self.kind
            result.receipt["receipt_status"] = "written_pending_import"
        return result
