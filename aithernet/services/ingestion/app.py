"""Ingestion-service HTTP API (Stage 14E, Part 25).

Node-facing ``/ingest/v1/*`` endpoints authenticate each request as a tenant+node via a signed
request; administrative dataset endpoints require a separate operator admin token and are NOT
exposed to nodes. No endpoint permits querying another tenant's records. Runs over localhost /
a private network — no public Internet required.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

from services.ingestion.config import IngestionConfig
from services.ingestion.service import IngestionError, IngestionService


def create_app(config: IngestionConfig | None = None,
               service: IngestionService | None = None) -> FastAPI:
    config = config or IngestionConfig.from_env()
    svc = service or IngestionService(config)
    app = FastAPI(title="Aithernet Ingestion Service")
    app.state.service = svc
    app.state.config = config

    ingest = APIRouter(prefix="/ingest/v1", tags=["ingest"])
    admin = APIRouter(prefix="/ingest/admin", tags=["ingest-admin"])

    def _svc(request: Request) -> IngestionService:
        return request.app.state.service

    async def _auth(request: Request, target: str):
        body = await request.body()
        result = request.app.state.service.authenticate(
            headers={k: v for k, v in request.headers.items()},
            method=request.method, target=target, body=body,
        )
        if not result.ok:
            raise HTTPException(status_code=401, detail=f"auth_failed:{result.code}")
        return result, body

    def _require_admin(request: Request, authorization: str | None) -> None:
        token = request.app.state.config.admin_token
        if not token or authorization != f"Bearer {token}":
            raise HTTPException(status_code=403, detail="admin_token_required")

    # -- node-facing -----------------------------------------------------------

    @ingest.post("/batches", status_code=202)
    async def post_batch(request: Request) -> dict:
        result, body = await _auth(request, "/ingest/v1/batches")
        idem = request.headers.get("x-aithernet-idempotency", "")
        try:
            return _svc(request).ingest_batch(auth=result, idempotency_key=idem, body=body)
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @ingest.get("/batches/{batch_id}")
    async def get_batch(batch_id: str, request: Request) -> dict:
        result, _ = await _auth(request, f"/ingest/v1/batches/{batch_id}")
        try:
            return _svc(request).get_batch(result.tenant_id, batch_id)
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @ingest.get("/receipts/{receipt_id}")
    async def get_receipt(receipt_id: str, request: Request) -> dict:
        result, _ = await _auth(request, f"/ingest/v1/receipts/{receipt_id}")
        try:
            return _svc(request).get_receipt(result.tenant_id, receipt_id)
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @ingest.post("/deletions", status_code=201)
    async def post_deletion(request: Request) -> dict:
        result, body = await _auth(request, "/ingest/v1/deletions")
        import json
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc
        try:
            return _svc(request).request_deletion(
                tenant_id=result.tenant_id, scope=payload.get("scope", "batch"),
                target_id=payload.get("batch_id") or payload.get("target_id"),
            )
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @ingest.get("/deletions/{deletion_id}")
    async def get_deletion(deletion_id: str, request: Request) -> dict:
        result, _ = await _auth(request, f"/ingest/v1/deletions/{deletion_id}")
        try:
            return _svc(request).get_deletion(result.tenant_id, deletion_id)
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @ingest.get("/readiness")
    async def readiness(request: Request) -> dict:
        return _svc(request).readiness()

    @ingest.get("/diagnostics")
    async def diagnostics(request: Request) -> dict:
        return _svc(request).diagnostics()

    # -- admin (separate auth) -------------------------------------------------

    @admin.post("/tenants", status_code=201)
    def admin_enroll_tenant(payload: dict, request: Request,
                            authorization: str | None = Header(default=None)) -> dict:
        _require_admin(request, authorization)
        return _svc(request).enroll_tenant(payload["tenant_id"], payload.get("name", "tenant"))

    @admin.post("/nodes", status_code=201)
    def admin_enroll_node(payload: dict, request: Request,
                          authorization: str | None = Header(default=None)) -> dict:
        _require_admin(request, authorization)
        try:
            return _svc(request).enroll_node(
                payload["tenant_id"], payload["node_id"],
                key_id=payload.get("key_id", "default"), public_key=payload["public_key"],
            )
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @admin.post("/credentials/revoke")
    def admin_revoke(payload: dict, request: Request,
                     authorization: str | None = Header(default=None)) -> dict:
        _require_admin(request, authorization)
        try:
            return _svc(request).revoke_credential(
                payload["tenant_id"], payload["node_id"], payload.get("key_id", "default")
            )
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @admin.post("/datasets", status_code=201)
    def admin_create_dataset(payload: dict, request: Request,
                             authorization: str | None = Header(default=None)) -> dict:
        _require_admin(request, authorization)
        return _svc(request).create_dataset(payload["tenant_id"], payload.get("name", "dataset"))

    @admin.post("/datasets/{dataset_id}/versions", status_code=201)
    def admin_create_version(dataset_id: str, payload: dict, request: Request,
                             authorization: str | None = Header(default=None)) -> dict:
        _require_admin(request, authorization)
        try:
            return _svc(request).create_dataset_version(payload["tenant_id"], dataset_id)
        except IngestionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    app.include_router(ingest)
    app.include_router(admin)
    return app


# Provided for symmetry with the node API (unused placeholder dependency).
def get_service(request: Request) -> IngestionService:  # pragma: no cover
    return request.app.state.service


_ = Depends  # keep the import referenced for parity with other routers
