"""beta.9 — hosted research / owner-archive ingestion (control-plane).

Enrolled nodes upload SANITIZED research packages (signed with their node identity + a scoped
Research Upload Capability) into the OWNER/ADMIN archive. This mixin owns:

- Research Upload Capability minting/verification/revocation (scoped, revocable, expiring; the raw
  token is shown once and only its digest is stored). For the beta program a capability is
  auto-approved for an enrolled node that explicitly consents, when the tenant policy allows it.
- Node package ingestion: verify capability + consent + schema + size/rate, run SERVER-SIDE secret
  scanning (defence-in-depth over the client scanner), deduplicate, store the payload in the hosted
  blob store, index metadata, and enqueue an owner-archive job. Suspicious packages are quarantined
  (metadata + category only) and are NEVER archived.
- Owner archive worker + server-side owner Google Drive connection (the owner's OAuth token lives
  ONLY here, is never returned to any client API, and clients never manage it).

No client Google credentials are ever involved. No secret is ever stored or returned: capability
tokens as digests only; the owner refresh token server-side only.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from services.control_plane.errors import (
    ControlPlaneError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
)
from services.control_plane.models import (
    HostedOwnerArchiveConnection,
    HostedResearchArchiveJob,
    HostedResearchArchiveLedger,
    HostedResearchCapability,
    HostedResearchCollectionPolicy,
    HostedResearchOAuthState,
    HostedResearchPackage,
    HostedResearchPackageArtifact,
    HostedResearchQuarantine,
)
from services.control_plane.roles import RESEARCH_ARCHIVE_MANAGE
from services.control_plane.security import (
    constant_time_equals,
    generate_token,
    token_digest,
)
from services.control_plane.timeutil import ensure_aware

RESEARCH_CAPABILITY_HEADER = "x-aithernet-research-capability"
DEFAULT_CAPABILITY_SCOPES = ["research.package.upload"]
DEFAULT_CAPABILITY_TTL_DAYS = 90
#: Hard server-side ceiling on a research package, independent of any capability value.
MAX_RESEARCH_PACKAGE_BYTES = 16 * 1024 * 1024
RESEARCH_PACKAGE_SCHEMAS = ("aithernet.dataset.snapshot.v1", "aithernet.research.package.v1")

# Extra key-name substrings the server refuses to archive even if the client somehow forwarded
# them — defence-in-depth on top of aithernet.data.redaction.contains_residual_secret.
_FORBIDDEN_KEY_SUBSTRINGS = (
    "api_key", "apikey", "authorization", "bearer", "refresh_token", "access_token",
    "vnc_password", "catgpt_gateway_api_key", "gateway_api_key", "cookie", "session_token",
    "private_key", "client_secret", "password", "credential",
)

# Owner-Drive OAuth state TTL (short — the admin completes consent immediately).
OAUTH_STATE_TTL_MINUTES = 15
#: Error categories from the Drive/OAuth layer that mean the owner must RECONNECT (revoked/expired
#: token or lost permission). The archive worker leaves jobs queued and flags the connection so the
#: admin portal prompts a reconnect — clients never see this and never take action.
_RECONNECT_CATEGORIES = frozenset({"expired_credentials", "unauthorized", "permission_denied"})
#: Categories that mean Drive is transiently/structurally unavailable (retry after operator action)
#: — jobs stay queued, connection flagged, but it is not necessarily a revoked token.
_DRIVE_UNAVAILABLE_CATEGORIES = frozenset({
    "token_refresh_failed", "token_endpoint_unreachable", "oauth_not_configured",
    "quota_exceeded", "drive_error", "not_found",
})


class ResearchMixin:
    # -- collection policy -----------------------------------------------------
    def _effective_policy(self, s, tenant_id: str) -> HostedResearchCollectionPolicy | None:
        row = s.execute(
            select(HostedResearchCollectionPolicy).where(
                HostedResearchCollectionPolicy.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if row is None:
            row = s.execute(
                select(HostedResearchCollectionPolicy).where(
                    HostedResearchCollectionPolicy.tenant_id == "*")
            ).scalar_one_or_none()
        return row

    def get_collection_policy(self, principal, *, tenant_id: str = "*") -> dict:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            row = self._effective_policy(s, tenant_id)
            if row is None:
                return {"tenant_id": tenant_id, "collection_allowed": True,
                        "auto_approve_beta": True, "max_package_bytes": 8 * 1024 * 1024,
                        "retention_days": 30, "version": 0, "default": True}
            return self._policy_dict(row)

    def set_collection_policy(self, principal, *, tenant_id: str = "*", collection_allowed=None,
                              auto_approve_beta=None, max_package_bytes=None,
                              retention_days=None) -> dict:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            row = s.execute(
                select(HostedResearchCollectionPolicy).where(
                    HostedResearchCollectionPolicy.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if row is None:
                row = HostedResearchCollectionPolicy(tenant_id=tenant_id, created_at=self.now())
                s.add(row)
            if collection_allowed is not None:
                row.collection_allowed = bool(collection_allowed)
            if auto_approve_beta is not None:
                row.auto_approve_beta = bool(auto_approve_beta)
            if max_package_bytes is not None:
                row.max_package_bytes = int(max_package_bytes)
            if retention_days is not None:
                row.retention_days = int(retention_days)
            row.version = (row.version or 0) + 1
            row.updated_at = self.now()
            self._audit(s, action="research.policy.set", actor_user_id=principal.user_id,
                        tenant_id=tenant_id)
            s.flush()
            out = self._policy_dict(row)
            s.commit()
            return out

    @staticmethod
    def _policy_dict(row: HostedResearchCollectionPolicy) -> dict:
        return {"tenant_id": row.tenant_id, "version": row.version,
                "collection_allowed": row.collection_allowed,
                "auto_approve_beta": row.auto_approve_beta,
                "max_package_bytes": row.max_package_bytes, "retention_days": row.retention_days}

    # -- capability minting / verification -------------------------------------
    def mint_research_capability(self, auth, body: dict | None = None) -> dict:
        """Mint (or rotate) a Research Upload Capability for the calling enrolled node.

        ``auth`` is the NodeAuthResult from node signed-request auth. Requires explicit consent
        (``body['consent'] is True``). Auto-approved for enrolled beta nodes when the tenant policy
        allows. Returns the raw capability token ONCE (only its digest is stored)."""
        body = body or {}
        if body.get("consent") is not True:
            raise ForbiddenError("consent_required")
        with self._session() as s:
            policy = self._effective_policy(s, auth.tenant_id)
            allowed = True if policy is None else policy.collection_allowed
            auto = True if policy is None else policy.auto_approve_beta
            if not allowed:
                raise ForbiddenError("collection_not_allowed")
            if not auto:
                raise ForbiddenError("capability_requires_admin_approval")
            max_bytes = 8 * 1024 * 1024 if policy is None else policy.max_package_bytes
            pol_version = 1 if policy is None else policy.version
            raw = generate_token(32)
            digest = token_digest(raw)
            existing = s.execute(
                select(HostedResearchCapability).where(
                    HostedResearchCapability.tenant_id == auth.tenant_id,
                    HostedResearchCapability.node_id == auth.node_id)
            ).scalar_one_or_none()
            expires = self.now() + timedelta(days=DEFAULT_CAPABILITY_TTL_DAYS)
            if existing is None:
                existing = HostedResearchCapability(
                    tenant_id=auth.tenant_id, node_id=auth.node_id,
                    node_pk=getattr(auth, "node_pk", None), created_at=self.now())
                s.add(existing)
            existing.token_digest = digest
            existing.scopes_json = list(DEFAULT_CAPABILITY_SCOPES)
            existing.policy_version = pol_version
            existing.max_package_bytes = max_bytes
            existing.status = "active"
            existing.revoked_reason = None
            existing.expires_at = expires
            self._audit(s, action="research.capability.mint", tenant_id=auth.tenant_id,
                        actor_kind="node", target=auth.node_id)
            s.flush()
            out = {
                "capability_token": raw,          # shown once
                "scopes": existing.scopes_json,
                "policy_version": existing.policy_version,
                "max_package_bytes": existing.max_package_bytes,
                "expires_at": existing.expires_at.isoformat() if existing.expires_at else None,
                "owner_archive_upload": "enabled",
            }
            s.commit()
            return out

    def _verify_capability(self, s, auth, token: str | None) -> HostedResearchCapability:
        if not token:
            raise ForbiddenError("capability_required")
        cap = s.execute(
            select(HostedResearchCapability).where(
                HostedResearchCapability.tenant_id == auth.tenant_id,
                HostedResearchCapability.node_id == auth.node_id,
                HostedResearchCapability.status == "active")
        ).scalar_one_or_none()
        if cap is None:
            raise ForbiddenError("capability_not_found")
        if not constant_time_equals(token_digest(token), cap.token_digest):
            raise ForbiddenError("capability_invalid")
        exp = ensure_aware(cap.expires_at)
        if exp is not None and exp < self.now():
            raise ForbiddenError("capability_expired")
        if "research.package.upload" not in (cap.scopes_json or []):
            raise ForbiddenError("capability_scope_denied")
        return cap

    def revoke_capability(self, principal, *, tenant_id: str, node_id: str,
                          reason: str = "admin_revoked") -> dict:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            cap = s.execute(
                select(HostedResearchCapability).where(
                    HostedResearchCapability.tenant_id == tenant_id,
                    HostedResearchCapability.node_id == node_id)
            ).scalar_one_or_none()
            if cap is None:
                raise NotFoundError("capability_not_found")
            cap.status = "revoked"
            cap.revoked_reason = reason[:120]
            self._audit(s, action="research.capability.revoke", actor_user_id=principal.user_id,
                        tenant_id=tenant_id, target=node_id)
            s.commit()
        return {"tenant_id": tenant_id, "node_id": node_id, "status": "revoked"}

    # -- server-side secret scanning ------------------------------------------
    @staticmethod
    def _scan_for_secrets(manifest: dict, records: list) -> tuple[bool, str, str]:
        """Return (clean, category, detail). Defence-in-depth over the client scanner."""
        import json as _json

        from aithernet.data.redaction import contains_residual_secret
        blob = {"manifest": manifest, "records": records}
        if contains_residual_secret(blob):
            return False, "residual_secret", "a residual secret pattern survived to the server"
        text = _json.dumps(blob, default=str).lower()
        for sub in _FORBIDDEN_KEY_SUBSTRINGS:
            if f'"{sub}"' in text or f"{sub}=" in text or f"{sub}:" in text:
                return False, "forbidden_key", f"forbidden key-like token: {sub}"
        return True, "", ""

    # -- ingestion -------------------------------------------------------------
    def ingest_research_package(self, auth, *, body: bytes, capability_token: str | None,
                                idempotency_key: str | None = None) -> dict:
        """Accept one sanitized research package from an enrolled, consented node.

        Verifies capability + size + schema, runs server-side secret scanning, deduplicates, stores
        the payload, indexes metadata, and enqueues an owner-archive job. Returns an ACK. Suspicious
        packages are quarantined and never archived. NEVER stores or returns a secret."""
        from aithernet.data.batch import open_bundle
        from aithernet.data.crypto import sha256_hex

        if not body:
            raise ControlPlaneError("empty_package")
        if len(body) > MAX_RESEARCH_PACKAGE_BYTES:
            raise ControlPlaneError("package_too_large", status=413)
        package_sha = "sha256:" + sha256_hex(body)

        with self._session() as s:
            cap = self._verify_capability(s, auth, capability_token)
            if len(body) > cap.max_package_bytes:
                raise ControlPlaneError("package_too_large", status=413)
            # bounded rate limit: packages accepted in the last hour for this node
            since = self.now() - timedelta(hours=1)
            recent = s.execute(
                select(func.count(HostedResearchPackage.id)).where(
                    HostedResearchPackage.tenant_id == auth.tenant_id,
                    HostedResearchPackage.node_id == auth.node_id,
                    HostedResearchPackage.received_at >= since)
            ).scalar() or 0
            if recent >= cap.rate_limit_per_hour:
                raise RateLimitError("research_rate_limited")

            # dedup by (tenant, package_sha256)
            dup = s.execute(
                select(HostedResearchPackage).where(
                    HostedResearchPackage.tenant_id == auth.tenant_id,
                    HostedResearchPackage.package_sha256 == package_sha)
            ).scalar_one_or_none()
            if dup is not None:
                return {"package_id": dup.id, "status": dup.status, "duplicate": True,
                        "package_sha256": package_sha, "archive": "queued"}

            try:
                manifest, records = open_bundle(body)
            except Exception as exc:  # noqa: BLE001 — sanitized category, no payload/traceback leak
                raise ControlPlaneError("invalid_package") from exc
            schema = str(manifest.get("schema_version") or manifest.get("schema") or "")
            release_version = manifest.get("release_version")

            clean, category, detail = self._scan_for_secrets(manifest, records)
            cap.last_used_at = self.now()
            if not clean:
                s.add(HostedResearchQuarantine(
                    tenant_id=auth.tenant_id, node_id=auth.node_id, package_sha256=package_sha,
                    reason_category=category, detail=detail[:200], created_at=self.now()))
                s.add(HostedResearchArchiveLedger(
                    package_id="", tenant_id=auth.tenant_id, node_id=auth.node_id,
                    action="quarantined", detail=category, created_at=self.now()))
                self._audit(s, action="research.package.quarantine", tenant_id=auth.tenant_id,
                            actor_kind="node", target=auth.node_id)
                s.commit()
                return {"status": "quarantined", "reason": category,
                        "package_sha256": package_sha, "duplicate": False, "archive": "blocked"}

            rel = f"research/{auth.tenant_id}/{auth.node_id}/{package_sha.split(':', 1)[1]}.bin"
            try:
                self.blob_store.put(rel, body)
            except Exception as exc:  # noqa: BLE001
                raise ControlPlaneError("storage_failed", status=503) from exc

            pkg = HostedResearchPackage(
                tenant_id=auth.tenant_id, node_id=auth.node_id,
                node_pk=getattr(auth, "node_pk", None), package_sha256=package_sha,
                schema_version=schema, release_version=release_version,
                record_count=int(manifest.get("record_count") or len(records)),
                byte_size=len(body), status="accepted", object_ref=rel,
                consent_state="granted",
                idempotency_key=(idempotency_key or manifest.get("idempotency_key")),
                received_at=self.now())
            s.add(pkg)
            s.flush()
            for art in self._artifact_manifest_from_records(records):
                s.add(HostedResearchPackageArtifact(
                    package_id=pkg.id, created_at=self.now(), **art))
            s.add(HostedResearchArchiveJob(
                package_id=pkg.id, tenant_id=auth.tenant_id, node_id=auth.node_id,
                status="queued", created_at=self.now()))
            s.add(HostedResearchArchiveLedger(
                package_id=pkg.id, tenant_id=auth.tenant_id, node_id=auth.node_id,
                action="accepted", detail=f"records={pkg.record_count}", created_at=self.now()))
            self._audit(s, action="research.package.accept", tenant_id=auth.tenant_id,
                        actor_kind="node", target=auth.node_id)
            pid = pkg.id
            s.commit()
            return {"package_id": pid, "status": "accepted", "duplicate": False,
                    "package_sha256": package_sha, "record_count": pkg.record_count,
                    "archive": "queued"}

    @staticmethod
    def _artifact_manifest_from_records(records: list) -> list[dict]:
        """Pull safe artifact manifest entries (path + hash only) out of the records."""
        out: list[dict] = []
        for rec in records if isinstance(records, list) else []:
            arts = rec.get("artifacts") if isinstance(rec, dict) else None
            for a in arts if isinstance(arts, list) else []:
                if not isinstance(a, dict):
                    continue
                out.append({
                    "relative_path": str(a.get("relative_path") or a.get("path") or "")[:400],
                    "content_hash": a.get("content_hash") or a.get("digest"),
                    "size_bytes": a.get("size_bytes") if isinstance(
                        a.get("size_bytes"), int) else None,
                    "media_type": a.get("media_type"),
                    "artifact_kind": a.get("artifact_kind"),
                })
                if len(out) >= 200:
                    return out
        return out

    # -- owner Google Drive connection (SERVER-SIDE only) ----------------------
    #
    # Two ways to connect, both storing the refresh token server-side ONLY (never returned):
    #   1. OAuth (the normal owner/admin flow): start_owner_drive_oauth -> Google consent ->
    #      complete_owner_drive_oauth. No token is ever pasted or shown.
    #   2. Manual refresh token (ADVANCED fallback, e.g. air-gapped provisioning) via
    #      connect_owner_drive.
    # The archive worker refreshes the token with the server-side Google OAuth client and writes
    # packages into the "Aithernet Research Archive" tree the app itself owns (drive.file scope).

    def _get_or_create_connection(self, s, tenant_id: str) -> HostedOwnerArchiveConnection:
        row = s.execute(
            select(HostedOwnerArchiveConnection).where(
                HostedOwnerArchiveConnection.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if row is None:
            row = HostedOwnerArchiveConnection(tenant_id=tenant_id, created_at=self.now())
            s.add(row)
        return row

    def start_owner_drive_oauth(self, principal, *, account_label: str | None = None,
                                tenant_id: str = "*") -> dict:
        """Begin the admin owner-Drive OAuth flow: mint a single-use CSRF state bound to this admin
        and return the Google authorization URL. No secret is returned; the client secret never
        leaves the server."""
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        from services.control_plane.owner_drive_oauth import build_authorize_url, resolve_client
        client = resolve_client(self.config)
        if client is None:
            raise ControlPlaneError("google_oauth_not_configured", status=503)
        state = generate_token(24)
        label = (account_label or "").strip()[:120] or None
        with self._session() as s:
            s.add(HostedResearchOAuthState(
                state=state, user_id=principal.user_id, tenant_id=tenant_id,
                account_label=label, redirect_uri=client.redirect_uri,
                expires_at=self.now() + timedelta(minutes=OAUTH_STATE_TTL_MINUTES),
                created_at=self.now()))
            self._audit(s, action="research.drive.oauth.start", actor_user_id=principal.user_id)
            s.commit()
        login_hint = label if label and "@" in label else None
        url = build_authorize_url(client, state=state, login_hint=login_hint)
        return {"authorize_url": url, "tenant_id": tenant_id}

    def complete_owner_drive_oauth(self, principal, *, code: str, state: str,
                                   tenant_id: str = "*") -> dict:
        """Finish the OAuth flow: validate the state against this admin, exchange the code
        server-side, store the refresh token server-side ONLY, verify Drive + ensure the archive
        root, and mark the connection connected. Never returns a token."""
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        from services.control_plane.owner_drive_oauth import (
            OwnerDriveOAuthError,
            ServerDriveOAuth,
            exchange_code,
            fetch_account_email,
            resolve_client,
        )
        client = resolve_client(self.config)
        if client is None:
            raise ControlPlaneError("google_oauth_not_configured", status=503)
        # 1. Validate + consume the state (single-use, bound to this admin). Consume BEFORE the
        #    network exchange so a replayed/duplicated callback cannot re-run it.
        with self._session() as s:
            st = s.execute(
                select(HostedResearchOAuthState).where(
                    HostedResearchOAuthState.state == state)
            ).scalar_one_or_none()
            if st is None or st.consumed:
                raise ForbiddenError("oauth_state_invalid")
            exp = ensure_aware(st.expires_at)
            if exp is not None and exp < self.now():
                raise ForbiddenError("oauth_state_expired")
            if st.user_id != principal.user_id:
                raise ForbiddenError("oauth_state_mismatch")
            st.consumed = True
            account_label = st.account_label
            s.commit()
        # 2. Exchange the code server-side (outside any DB transaction). Sanitized errors only.
        try:
            tokens = exchange_code(client, code=code)
        except OwnerDriveOAuthError as exc:
            raise ControlPlaneError(f"oauth_{exc.category}", status=502) from None
        refresh_token = tokens["refresh_token"]
        # 3. Verify Drive access + ensure the archive root folder; fetch the account email (best
        #    effort). If Drive is briefly unavailable we still store the token — the worker will
        #    create the folders on the first sync.
        account_email = fetch_account_email(tokens.get("access_token") or "")
        root_id: str | None = None
        try:
            from aithernet.data.destinations.google_drive_client import RealGoogleDriveClient
            rest = RealGoogleDriveClient(ServerDriveOAuth(client, refresh_token))
            root_id = rest.find_or_create_folder(client.archive_root_name, None)
        except Exception:  # noqa: BLE001 — folder creation deferred to the worker; token still valid
            root_id = None
        # 4. Persist server-side ONLY. The refresh token is never returned.
        with self._session() as s:
            row = self._get_or_create_connection(s, tenant_id)
            row.refresh_token = refresh_token
            row.status = "connected"
            row.scope = tokens.get("scope") or client.scope
            row.account_email = account_email
            row.account_label = account_label or account_email
            row.root_folder_id = root_id
            row.root_folder_name = client.archive_root_name
            row.error_category = None
            row.connected_via = "oauth"
            row.connected_at = self.now()
            self._audit(s, action="research.drive.oauth.connect", actor_user_id=principal.user_id,
                        target=(account_email or account_label or "owner")[:120])
            s.commit()
        return self.owner_drive_status(principal, tenant_id=tenant_id)

    def connect_owner_drive(self, principal, *, refresh_token: str, root_folder_id=None,
                            account_label=None, tenant_id: str = "*") -> dict:
        """ADVANCED fallback: store an operator-provided Drive refresh token SERVER-SIDE.

        The normal owner/admin path is the OAuth flow (no token pasting). This exists for
        provisioning cases where a refresh token is minted out of band. Never returns the token."""
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        if not refresh_token:
            raise ControlPlaneError("refresh_token_required")
        with self._session() as s:
            row = self._get_or_create_connection(s, tenant_id)
            row.refresh_token = refresh_token
            row.root_folder_id = root_folder_id
            row.root_folder_name = self.config.google_oauth.archive_root_name
            row.account_label = (account_label or "")[:120] or None
            row.scope = self.config.google_oauth.scope
            row.status = "connected"
            row.error_category = None
            row.connected_via = "manual"
            row.connected_at = self.now()
            self._audit(s, action="research.drive.connect", actor_user_id=principal.user_id,
                        target=(account_label or "owner")[:120])
            s.commit()
        return {"status": "connected", "tenant_id": tenant_id}  # token never returned

    def disconnect_owner_drive(self, principal, *, tenant_id: str = "*") -> dict:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            row = s.execute(
                select(HostedOwnerArchiveConnection).where(
                    HostedOwnerArchiveConnection.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if row is not None:
                row.status = "disconnected"
                row.refresh_token = None  # forget the server-side secret entirely
                row.error_category = None
                row.connected_via = None
                self._audit(s, action="research.drive.disconnect", actor_user_id=principal.user_id)
                s.commit()
        return {"status": "disconnected", "tenant_id": tenant_id}

    def _job_counts(self, s) -> dict:
        def _c(status):
            return s.execute(
                select(func.count(HostedResearchArchiveJob.id)).where(
                    HostedResearchArchiveJob.status == status)).scalar() or 0
        return {"queued_jobs": _c("queued"), "synced_jobs": _c("synced"),
                "failed_jobs": _c("failed")}

    def owner_drive_status(self, principal, *, tenant_id: str = "*") -> dict:
        """Bounded, secret-free owner-Drive connection status for the admin portal.

        NEVER returns the refresh/access token, client secret, raw OAuth response, or file
        contents — only whether it is connected, the account label/email, root folder id/name, last
        sync, a bounded error category, and the queued/synced/failed job counts."""
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        oauth_configured = self.config.google_oauth_configured
        with self._session() as s:
            row = s.execute(
                select(HostedOwnerArchiveConnection).where(
                    HostedOwnerArchiveConnection.tenant_id == tenant_id)
            ).scalar_one_or_none()
            counts = self._job_counts(s)
            if row is not None and row.status == "connected":
                status = "connected"
            elif row is not None and row.status == "error":
                status = "error"
            elif not oauth_configured:
                status = "unconfigured"
            else:
                status = "disconnected"
            return {
                "status": status,
                "connected": status == "connected",
                "tenant_id": tenant_id,
                "oauth_configured": oauth_configured,
                "scope": (row.scope if row and row.scope else self.config.google_oauth.scope),
                "account_label": row.account_label if row else None,
                "account_email": getattr(row, "account_email", None) if row else None,
                "root_folder_id": row.root_folder_id if row else None,
                "root_folder_name": getattr(row, "root_folder_name", None) if row else None,
                "connected_via": getattr(row, "connected_via", None) if row else None,
                "error_category": getattr(row, "error_category", None) if row else None,
                "last_sync_at": row.last_sync_at.isoformat() if row and row.last_sync_at else None,
                **counts,
            }

    # -- archive worker --------------------------------------------------------
    def process_archive_jobs(self, principal=None, *, limit: int = 50, tenant_id: str = "*",
                             drive_writer=None) -> dict:
        """Write queued packages into the owner Drive archive. This is the AUTOMATIC steady-state
        worker (the admin "Process archive jobs" button is only for manual retry/testing). If Drive
        is not connected the jobs stay ``queued`` (client uploads still succeeded — archiving is a
        server-side concern; clients never fail or act). A revoked/expired token flags the
        connection so the owner is prompted to reconnect ONCE, and jobs stay queued for after.

        One owner Drive connection serves ALL consenting/enrolled nodes and tenants — there is no
        per-node or per-mission Drive setup. ``drive_writer(package)->file_id`` is injectable for
        tests; production builds it from the stored server-side connection."""
        if principal is not None:
            self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        synced = failed = 0
        degraded: str | None = None
        with self._session() as s:
            conn = s.execute(
                select(HostedOwnerArchiveConnection).where(
                    HostedOwnerArchiveConnection.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if drive_writer is not None:
                writer, reason = drive_writer, None
            else:
                writer, reason = self._resolve_drive_writer(conn)
            if writer is None:
                queued = s.execute(
                    select(func.count(HostedResearchArchiveJob.id)).where(
                        HostedResearchArchiveJob.status == "queued")
                ).scalar() or 0
                if conn is not None and reason and conn.status not in ("disconnected",):
                    conn.status = "error"
                    conn.error_category = reason
                    s.commit()
                return {"synced": 0, "failed": 0, "skipped": queued,
                        "reason": reason or "owner_drive_not_connected", "queued": queued}
            jobs = s.execute(
                select(HostedResearchArchiveJob).where(
                    HostedResearchArchiveJob.status == "queued").limit(limit)
            ).scalars().all()
            for job in jobs:
                pkg = s.get(HostedResearchPackage, job.package_id)
                if pkg is None:
                    job.status = "failed"
                    job.last_error_type = "package_missing"
                    failed += 1
                    continue
                try:
                    file_id = writer(pkg)
                except Exception as exc:  # noqa: BLE001 — classify by bounded category
                    category = getattr(exc, "category", None)
                    if category in _RECONNECT_CATEGORIES:
                        degraded = "reconnect_required"
                        break  # leave this + remaining jobs queued for after reconnect
                    if category in _DRIVE_UNAVAILABLE_CATEGORIES:
                        degraded = "drive_unavailable"
                        break
                    job.status = "failed"
                    job.attempts = (job.attempts or 0) + 1
                    job.last_error_type = type(exc).__name__
                    s.add(HostedResearchArchiveLedger(
                        package_id=pkg.id, tenant_id=pkg.tenant_id, node_id=pkg.node_id,
                        action="archive_failed", detail=type(exc).__name__, created_at=self.now()))
                    failed += 1
                    continue
                job.status = "synced"
                job.drive_file_id = file_id
                job.updated_at = self.now()
                s.add(HostedResearchArchiveLedger(
                    package_id=pkg.id, tenant_id=pkg.tenant_id, node_id=pkg.node_id,
                    action="archived", drive_file_id=file_id, created_at=self.now()))
                synced += 1
            if conn is not None:
                if degraded:
                    conn.status = "error"
                    conn.error_category = degraded
                elif synced:
                    conn.last_sync_at = self.now()
                    conn.status = "connected"
                    conn.error_category = None
            s.commit()
        out = {"synced": synced, "failed": failed, "skipped": 0}
        if degraded:
            out["reason"] = degraded
        return out

    def _resolve_drive_writer(self, conn):
        """Return ``(writer, None)`` when the owner Drive is usable, else ``(None, reason)``.

        The writer lazily refreshes the token and creates the archive hierarchy on first use, so
        an expired/revoked token surfaces as an :class:`OwnerDriveOAuthError` at write time (which
        the worker maps to a reconnect-required state) rather than here."""
        if conn is None or conn.status == "disconnected" or not conn.refresh_token:
            return None, None
        from services.control_plane.owner_drive_oauth import ServerDriveOAuth, resolve_client
        client = resolve_client(self.config)
        if client is None:
            return None, "oauth_not_configured"
        try:
            from aithernet.data.destinations.google_drive_client import RealGoogleDriveClient
        except Exception:  # noqa: BLE001
            return None, "drive_client_unavailable"
        rest = RealGoogleDriveClient(ServerDriveOAuth(client, conn.refresh_token))
        root_name = client.archive_root_name
        folder_cache: dict = {}

        def _folder(name: str, parent: str | None) -> str:
            key = (name, parent)
            if key not in folder_cache:
                folder_cache[key] = rest.find_or_create_folder(name, parent)
            return folder_cache[key]

        def _write(pkg) -> str:
            # <root>/tenants/<tenant>/nodes/<node>/packages/<sha>.bin — one shared archive tree for
            # every tenant/node; idempotent by package sha (a retry resolves to the same file).
            root = _folder(root_name, None)
            tenants = _folder("tenants", root)
            t_folder = _folder(pkg.tenant_id, tenants)
            nodes = _folder("nodes", t_folder)
            n_folder = _folder(pkg.node_id, nodes)
            packages = _folder("packages", n_folder)
            data = self.blob_store.get(pkg.object_ref)
            sha = pkg.package_sha256.split(":", 1)[1] if ":" in pkg.package_sha256 \
                else pkg.package_sha256
            up = rest.resumable_upload(
                folder_id=packages, name=f"{sha}.bin", data=data,
                idempotency_key=pkg.package_sha256, chunk_bytes=8 * 1024 * 1024)
            return up.get("drive_file_id") or up.get("id") or ""

        return _write, None

    # -- admin + customer read views ------------------------------------------
    def research_admin_status(self, principal, *, tenant_id: str = "*") -> dict:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            def _count(model, **where):
                stmt = select(func.count(model.id))
                for k, v in where.items():
                    stmt = stmt.where(getattr(model, k) == v)
                return s.execute(stmt).scalar() or 0
            drive = self.owner_drive_status(principal, tenant_id=tenant_id)
            return {
                "owner_drive": drive,
                "packages_accepted": _count(HostedResearchPackage, status="accepted"),
                "quarantined": _count(HostedResearchQuarantine),
                "archive_jobs_queued": _count(HostedResearchArchiveJob, status="queued"),
                "archive_jobs_synced": _count(HostedResearchArchiveJob, status="synced"),
                "archive_jobs_failed": _count(HostedResearchArchiveJob, status="failed"),
                "capabilities_active": _count(HostedResearchCapability, status="active"),
            }

    def research_list_packages(self, principal, *, limit: int = 50) -> list[dict]:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            rows = s.execute(
                select(HostedResearchPackage).order_by(
                    HostedResearchPackage.received_at.desc()).limit(limit)
            ).scalars().all()
            return [{"package_id": r.id, "tenant_id": r.tenant_id, "node_id": r.node_id,
                     "package_sha256": r.package_sha256, "record_count": r.record_count,
                     "byte_size": r.byte_size, "status": r.status,
                     "release_version": r.release_version,
                     "received_at": r.received_at.isoformat() if r.received_at else None}
                    for r in rows]

    def research_list_quarantine(self, principal, *, limit: int = 50) -> list[dict]:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            rows = s.execute(
                select(HostedResearchQuarantine).order_by(
                    HostedResearchQuarantine.created_at.desc()).limit(limit)
            ).scalars().all()
            return [{"id": r.id, "tenant_id": r.tenant_id, "node_id": r.node_id,
                     "reason_category": r.reason_category, "detail": r.detail,
                     "created_at": r.created_at.isoformat() if r.created_at else None}
                    for r in rows]

    def research_list_jobs(self, principal, *, status: str | None = None,
                           limit: int = 50) -> list[dict]:
        self.require_platform_permission(principal, RESEARCH_ARCHIVE_MANAGE)
        with self._session() as s:
            stmt = select(HostedResearchArchiveJob).order_by(
                HostedResearchArchiveJob.created_at.desc()).limit(limit)
            if status:
                stmt = stmt.where(HostedResearchArchiveJob.status == status)
            rows = s.execute(stmt).scalars().all()
            return [{"job_id": r.id, "package_id": r.package_id, "tenant_id": r.tenant_id,
                     "node_id": r.node_id, "status": r.status, "attempts": r.attempts,
                     "drive_file_id": r.drive_file_id, "last_error_type": r.last_error_type}
                    for r in rows]

    def research_node_summary(self, principal, *, tenant_id: str) -> list[dict]:
        """Per-node consent/upload status for a tenant's own nodes (customer read)."""
        from services.control_plane.roles import RESEARCH_READ_SUMMARY
        self.require_tenant_permission(principal, tenant_id, RESEARCH_READ_SUMMARY)
        with self._session() as s:
            caps = s.execute(
                select(HostedResearchCapability).where(
                    HostedResearchCapability.tenant_id == tenant_id)
            ).scalars().all()
            out = []
            for c in caps:
                last = s.execute(
                    select(func.max(HostedResearchPackage.received_at)).where(
                        HostedResearchPackage.tenant_id == tenant_id,
                        HostedResearchPackage.node_id == c.node_id)
                ).scalar()
                total = s.execute(
                    select(func.count(HostedResearchPackage.id)).where(
                        HostedResearchPackage.tenant_id == tenant_id,
                        HostedResearchPackage.node_id == c.node_id,
                        HostedResearchPackage.status == "accepted")
                ).scalar() or 0
                out.append({"node_id": c.node_id, "consent": "granted",
                            "owner_archive_upload": ("enabled" if c.status == "active"
                                                     else c.status),
                            "packages_received": total,
                            "last_upload_at": last.isoformat() if last else None})
            return out
