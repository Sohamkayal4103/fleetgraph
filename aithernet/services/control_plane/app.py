"""Hosted control-plane HTTP API (Stage 14F §16, §29, §30).

Browser surfaces authenticate with a secure session cookie (HttpOnly, SameSite, Secure in
production) plus a double-submit CSRF token on state-changing requests. Node surfaces authenticate
with a signed request (never a website cookie). Every customer query is tenant-scoped server-side;
platform-admin routes enforce role authorization server-side, not by hiding buttons. No
authentication token is ever placed in browser localStorage.
"""

from __future__ import annotations

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response

from services.control_plane.config import HostedConfig
from services.control_plane.errors import AuthError, ControlPlaneError
from services.control_plane.service import ControlPlaneService, Principal

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def create_app(config: HostedConfig | None = None,
               service: ControlPlaneService | None = None) -> FastAPI:
    config = config or HostedConfig.from_env()
    svc = service or ControlPlaneService(config)
    app = FastAPI(title="Aithernet Control Plane")
    app.state.service = svc
    app.state.config = config

    # EXACT-ORIGIN CORS for the public static site (www.aithernet.online). Credentialed so the
    # public site can read the read-only session-status endpoint with the user's same-site cookie;
    # NEVER a wildcard (a wildcard with credentials is forbidden by browsers and unsafe). CSRF on
    # state-changing endpoints is still enforced independently via the X-CSRF-Token header, so
    # allowing credentials here does not weaken CSRF. Same-origin app traffic (browser on
    # app.aithernet.online -> /api) does not use this path.
    import os
    from urllib.parse import urlparse as _urlparse

    from fastapi.middleware.cors import CORSMiddleware

    _origins: set[str] = set()
    _public_origin = (config.urls.public_site or "").rstrip("/")
    if _public_origin:
        _origins.add(_public_origin)
        _u = _urlparse(_public_origin)
        if _u.scheme and _u.netloc.startswith("www."):  # also allow the apex if it is used
            _origins.add(f"{_u.scheme}://{_u.netloc[4:]}")
    for _extra in (os.environ.get("AITHERNET_HOSTED_CORS_ORIGINS", "") or "").split(","):
        _extra = _extra.strip().rstrip("/")
        if _extra:
            _origins.add(_extra)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "x-csrf-token"],
        max_age=600,
    )

    def _svc(request: Request) -> ControlPlaneService:
        return request.app.state.service

    # -- session principal + CSRF ------------------------------------------------------------

    def current(request: Request,
                session: str | None = Cookie(default=None,
                                             alias=config.sessions.cookie_name)) -> tuple:
        result = request.app.state.service.validate_session(session)
        if result is None:
            raise HTTPException(status_code=401, detail="not_authenticated")
        return result  # (principal, session_id, csrf_secret)

    def principal_only(auth: tuple = Depends(current)) -> Principal:
        return auth[0]

    def csrf_guarded(request: Request, auth: tuple = Depends(current),
                     x_csrf_token: str | None = Header(default=None)) -> Principal:
        if request.method not in _SAFE_METHODS:
            _, _, csrf_secret = auth
            if not request.app.state.service.verify_csrf(csrf_secret, x_csrf_token):
                raise HTTPException(status_code=403, detail="csrf_failed")
        return auth[0]

    def _handle(exc: ControlPlaneError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=exc.code)

    # -- node signed-request auth ------------------------------------------------------------

    async def node_auth(request: Request, target: str):
        body = await request.body()
        try:
            result = request.app.state.service.authenticate_node(
                headers=dict(request.headers), method=request.method, target=target, body=body)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        return result, body

    _register_health(app, _svc)
    _register_auth(app, _svc, config, current, csrf_guarded, _handle)
    _register_accounts(app, _svc, principal_only, csrf_guarded, current, _handle)
    _register_fleet(app, _svc, principal_only, csrf_guarded, node_auth, _handle)
    _register_releases(app, _svc, principal_only, csrf_guarded, node_auth, config, _handle)
    _register_support(app, _svc, principal_only, csrf_guarded, _handle)
    _register_admin(app, _svc, principal_only, csrf_guarded, current, config, _handle)
    _register_intake(app, _svc, principal_only, _handle)
    return app


def _register_intake(app, _svc, principal_only, _handle):
    """Public early-access + mailing-list intake (anti-enumeration; rate-limited; Turnstile)."""

    def _client_ip(request: Request) -> str | None:
        # Behind Cloudflare Tunnel the real client IP is CF-Connecting-IP; the origin is
        # loopback-only so only Cloudflare reaches it. Fall back conservatively.
        return (request.headers.get("cf-connecting-ip")
                or (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or None)
                or (request.client.host if request.client else None))

    @app.post("/v1/early-access/request")
    def early_access_request(payload: dict, request: Request) -> dict:
        return _svc(request).submit_early_access(
            email=payload.get("email", ""),
            privacy_ack=bool(payload.get("privacy_ack", False)),
            mailing_opt_in=bool(payload.get("mailing_opt_in", False)),
            turnstile_token=payload.get("turnstile_token"),
            client_ip=_client_ip(request))

    @app.post("/v1/mailing-list/subscribe")
    def mailing_subscribe(payload: dict, request: Request) -> dict:
        return _svc(request).subscribe_mailing(
            email=payload.get("email", ""),
            turnstile_token=payload.get("turnstile_token"),
            client_ip=_client_ip(request))

    @app.post("/v1/mailing-list/confirm")
    def mailing_confirm(payload: dict, request: Request) -> dict:
        return _svc(request).confirm_mailing(payload.get("token", ""))

    @app.post("/v1/mailing-list/unsubscribe")
    def mailing_unsubscribe(payload: dict, request: Request) -> dict:
        return _svc(request).unsubscribe_mailing(payload.get("token", ""))

    @app.get("/v1/admin/mailing-list/status")
    def mailing_status(request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).mailing_status(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc


# -- routers (split to keep each focused) --------------------------------------------------------


def _register_health(app, _svc):
    @app.get("/health/ready")
    def ready(request: Request) -> dict:
        return _svc(request).readiness()

    @app.get("/health/diagnostics")
    def diagnostics(request: Request) -> dict:
        return _svc(request).diagnostics()


def _register_auth(app, _svc, config, current, csrf_guarded, _handle):
    @app.post("/v1/auth/login")
    def login(payload: dict, request: Request, response: Response) -> dict:
        svc = _svc(request)
        client = request.client.host if request.client else "global"
        try:
            user = svc.authenticate(payload.get("email", ""), payload.get("password", ""),
                                    rate_key=client)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        session = svc.create_session(user["user_id"],
                                     user_agent=request.headers.get("user-agent"))
        _set_session_cookies(response, config, session)
        return {"user_id": user["user_id"], "email": user["email"],
                "csrf_token": session["csrf_token"]}

    @app.post("/v1/auth/logout")
    def logout(request: Request, response: Response, auth: tuple = Depends(current)) -> dict:
        _, session_id, _ = auth
        _svc(request).revoke_session(session_id)
        response.delete_cookie(config.sessions.cookie_name)
        response.delete_cookie(config.sessions.csrf_cookie_name)
        return {"logged_out": True}

    @app.get("/v1/auth/session")
    def whoami(request: Request, auth: tuple = Depends(current)) -> dict:
        principal, _, csrf_secret = auth
        return {"user_id": principal.user_id, "email": principal.email,
                "is_platform_admin": principal.is_platform_admin,
                "platform_roles": sorted(principal.platform_roles),
                "tenants": principal.tenant_ids(),
                "csrf_token": _svc(request).csrf_token_for(csrf_secret)}

    @app.get("/v1/auth/session-status")
    def session_status(
        request: Request,
        response: Response,
        session: str | None = Cookie(default=None, alias=config.sessions.cookie_name),
    ) -> dict:
        """Read-only, public-safe session status for the marketing site's account-aware header.

        Returns HTTP 200 for BOTH authenticated and anonymous requests (never 401), so a
        cross-origin status check from www.aithernet.online does not look like an error. It NEVER
        sets/refreshes a cookie or mutates the session, and returns NO session id, CSRF / OAuth /
        access / refresh token, tenant secret, or internal claim — only display name + email.
        """
        response.headers["Cache-Control"] = "no-store"
        return _svc(request).public_session_status(session)

    @app.post("/v1/auth/password-reset/request")
    def reset_request(payload: dict, request: Request) -> dict:
        return _svc(request).request_password_reset(payload.get("email", ""))

    @app.post("/v1/auth/password-reset/confirm")
    def reset_confirm(payload: dict, request: Request) -> dict:
        try:
            return _svc(request).confirm_password_reset(
                payload.get("token", ""), payload.get("password", ""))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/auth/sessions")
    def sessions(request: Request, principal=Depends(_principal_dep(current))) -> list:
        return _svc(request).list_sessions(principal)


def _register_accounts(app, _svc, principal_only, csrf_guarded, current, _handle):
    @app.get("/v1/tenants")
    def tenants(request: Request, principal=Depends(principal_only)) -> list:
        return _svc(request).list_tenants(principal)

    @app.get("/v1/memberships")
    def memberships(request: Request, principal=Depends(principal_only)) -> list:
        return _svc(request).list_memberships(principal)

    @app.post("/v1/invitations")
    def create_invitation(payload: dict, request: Request,
                          principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).create_invitation(
                principal, email=payload["email"], role=payload.get("role", "tenant_admin"),
                tenant_id=payload.get("tenant_id"),
                proposed_tenant_name=payload.get("proposed_tenant_name"),
                note=payload.get("note"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/invitations")
    def list_invitations(request: Request, principal=Depends(principal_only),
                         tenant_id: str | None = None) -> list:
        try:
            return _svc(request).list_invitations(principal, tenant_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/invitations/validate")
    def validate_invitation(request: Request, token: str = "") -> dict:
        # Public, NON-consuming: returns bounded invitation details for the public accept page.
        return _svc(request).validate_invitation(token)

    @app.post("/v1/invitations/accept")
    def accept_invitation(payload: dict, request: Request) -> dict:
        try:
            return _svc(request).accept_invitation(
                payload.get("token", ""), email=payload.get("email", ""),
                password=payload.get("password", ""),
                display_name=payload.get("display_name"),
                accept_required_policies=bool(payload.get("accept_required_policies", False)))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/invitations/{invitation_id}/revoke")
    def revoke_invitation(invitation_id: str, request: Request,
                          principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).revoke_invitation(principal, invitation_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/policies")
    def policies(request: Request, policy_type: str | None = None) -> list:
        return _svc(request).list_active_policies(policy_type)

    @app.post("/v1/policies")
    def publish_policy(payload: dict, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).publish_policy(
                principal, policy_type=payload["policy_type"], version=payload["version"],
                title=payload.get("title", ""), document_text=payload.get("document_text", ""),
                required_categories=payload.get("required_categories"),
                optional_categories=payload.get("optional_categories"),
                supersedes_version=payload.get("supersedes_version"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/policies/accept")
    def accept_policy(payload: dict, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).accept_policy(
                principal, policy_id=payload["policy_id"], tenant_id=payload.get("tenant_id"),
                optional_choices=payload.get("optional_choices"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/policies/status")
    def policy_status(request: Request, principal=Depends(principal_only)) -> dict:
        return _svc(request).acceptance_status(principal)


def _register_fleet(app, _svc, principal_only, csrf_guarded, node_auth, _handle):
    @app.post("/v1/enrollment-codes")
    def create_code(payload: dict, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).create_enrollment_code(
                principal, tenant_id=payload["tenant_id"],
                node_name_constraint=payload.get("node_name_constraint"),
                os_constraint=payload.get("os_constraint"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/enrollment-codes")
    def list_codes(request: Request, tenant_id: str,
                   principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).list_enrollment_codes(principal, tenant_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/enrollment-codes/{code_id}/revoke")
    def revoke_code(code_id: str, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).revoke_enrollment_code(principal, code_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/fleet/nodes")
    def fleet_nodes(request: Request, principal=Depends(principal_only),
                    tenant_id: str | None = None) -> list:
        try:
            return _svc(request).list_nodes(principal, tenant_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/fleet/nodes/{node_id}/rename")
    def fleet_rename(node_id: str, payload: dict, request: Request,
                     principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).rename_node(principal, node_id, payload.get("display_name", ""))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/meshes")
    def create_mesh(payload: dict, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).create_mesh(
                principal, tenant_id=payload["tenant_id"],
                display_name=payload.get("display_name", ""))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/meshes")
    def list_meshes(request: Request, tenant_id: str, principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).list_meshes(principal, tenant_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/meshes/{mesh_id}")
    def get_mesh(mesh_id: str, request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).get_mesh(principal, mesh_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/meshes/{mesh_id}/members")
    def add_mesh_member(mesh_id: str, payload: dict, request: Request,
                        principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).add_mesh_member(
                principal, mesh_id, hosted_node_id=payload["hosted_node_id"],
                role=payload.get("role", "member"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/meshes/{mesh_id}/members/{hosted_node_id}/revoke")
    def revoke_mesh_member(mesh_id: str, hosted_node_id: str, request: Request,
                           principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).revoke_mesh_member(principal, mesh_id, hosted_node_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/fleet/summary")
    def fleet_summary(request: Request, principal=Depends(principal_only),
                      tenant_id: str | None = None) -> dict:
        try:
            return _svc(request).fleet_summary(principal, tenant_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/fleet/nodes/{node_id}")
    def fleet_node(node_id: str, request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).get_node(principal, node_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/fleet/nodes/{node_id}/revoke")
    def fleet_revoke(node_id: str, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).revoke_node(principal, node_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/fleet/nodes/{node_id}/restore")
    def fleet_restore(node_id: str, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).restore_node(principal, node_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/fleet/nodes/{node_id}/heartbeats")
    def fleet_heartbeats(node_id: str, request: Request,
                         principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).node_heartbeats(principal, node_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    # -- node-facing (signed) -----

    @app.post("/v1/node/enroll/challenge")
    def enroll_challenge(payload: dict, request: Request) -> dict:
        client = request.client.host if request.client else "global"
        try:
            return _svc(request).enroll_challenge(
                code=payload.get("code", ""), public_key=payload.get("public_key", ""),
                rate_key=client)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/node/enroll")
    def enroll(payload: dict, request: Request) -> dict:
        client = request.client.host if request.client else "global"
        try:
            return _svc(request).enroll(
                code=payload.get("code", ""), public_key=payload.get("public_key", ""),
                node_id=payload.get("node_id", ""), signature=payload.get("signature", ""),
                software_version=payload.get("software_version"),
                display_name=payload.get("display_name"),
                os_family=payload.get("os_family"), rate_key=client)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/node/heartbeat")
    async def heartbeat(request: Request) -> dict:
        import json

        auth, body = await node_auth(request, "/v1/node/heartbeat")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc
        try:
            return _svc(request).record_heartbeat(auth, payload)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/node/config")
    async def node_config(request: Request) -> dict:
        auth, _ = await node_auth(request, "/v1/node/config")
        return _svc(request).node_config(auth)

    @app.get("/v1/node/release")
    async def node_release(request: Request) -> dict:
        await node_auth(request, "/v1/node/release")
        rel = _svc(request).channel_release()
        return rel or {"available": False}

    # -- node-facing research/owner-archive (signed) -----

    @app.post("/v1/node/research/capability")
    async def research_capability(request: Request) -> dict:
        import json

        auth, body = await node_auth(request, "/v1/node/research/capability")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc
        try:
            return _svc(request).mint_research_capability(auth, payload)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/node/research/packages")
    async def research_package(request: Request):
        from fastapi import Response as _Response
        auth, body = await node_auth(request, "/v1/node/research/packages")
        cap = request.headers.get("x-aithernet-research-capability")
        idem = request.headers.get("x-aithernet-idempotency")
        try:
            ack = _svc(request).ingest_research_package(
                auth, body=body, capability_token=cap, idempotency_key=idem)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        import json as _json
        return _Response(content=_json.dumps(ack), status_code=202,
                         media_type="application/json")

    @app.get("/v1/research/node-summary")
    def research_node_summary(request: Request, tenant_id: str,
                              principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).research_node_summary(principal, tenant_id=tenant_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc


def _register_releases(app, _svc, principal_only, csrf_guarded, node_auth, config, _handle):
    @app.get("/v1/releases")
    def releases(request: Request, principal=Depends(principal_only),
                 channel: str | None = None) -> list:
        return _svc(request).list_releases(principal, channel, include_unpublished=True)

    @app.post("/v1/releases")
    def create_release(payload: dict, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).create_release(
                principal, version=payload["version"], channel=payload.get("channel"),
                minimum_supported_version=payload.get("minimum_supported_version"),
                os_support=payload.get("os_support"), architecture=payload.get("architecture"),
                notes=payload.get("notes"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/releases/{release_id}/artifacts")
    async def add_artifact(release_id: str, request: Request,
                           principal=Depends(csrf_guarded)) -> dict:
        # Authenticated release-artifact upload — the one route granted a larger (but still bounded)
        # body limit by the reverse proxy. The bound is RE-ENFORCED here so it holds even if a proxy
        # is bypassed: reject by Content-Length first (before buffering), then by actual size.
        svc = _svc(request)
        max_bytes = svc.config.releases.max_artifact_bytes
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise HTTPException(status_code=413, detail="artifact_too_large")
        data = await request.body()
        if len(data) > max_bytes:
            raise HTTPException(status_code=413, detail="artifact_too_large")
        name = request.headers.get("x-artifact-name", "artifact.bin")
        size_hdr = request.headers.get("x-artifact-size")
        try:
            return svc.add_release_artifact(
                principal, release_id=release_id, name=name, data=data,
                kind=request.headers.get("x-artifact-kind"),  # None -> inferred from filename

                content_type=request.headers.get("content-type", "application/octet-stream"),
                expected_sha256=request.headers.get("x-artifact-sha256"),
                expected_byte_size=int(size_hdr) if size_hdr and size_hdr.isdigit() else None)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/releases/{release_id}/sign")
    def sign_release(release_id: str, payload: dict, request: Request,
                     principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).sign_release(
                principal, release_id=release_id,
                signing_seed_b64=payload.get("signing_seed_b64"),
                signing_key_id=payload.get("signing_key_id"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/releases/{release_id}/publish")
    def publish_release(release_id: str, request: Request,
                        principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).publish_release(principal, release_id=release_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/releases/{release_id}/revoke")
    def revoke_release(release_id: str, request: Request,
                       principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).revoke_release(principal, release_id=release_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/releases/{release_id}/manifest")
    def manifest(release_id: str, request: Request) -> dict:
        try:
            return _svc(request).release_manifest(release_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/public/releases")
    def public_releases(request: Request) -> dict:
        # Unauthenticated: published-release metadata + verification key only (no package bytes).
        return _svc(request).public_releases()

    @app.get("/v1/downloads/{release_id}/{name}")
    def download(release_id: str, name: str, request: Request) -> Response:
        # PUBLIC route now serves ONLY the verification key; package artifacts require auth.
        try:
            data, content_type, sha = _svc(request).download_artifact_public(
                release_id=release_id, name=name)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        return Response(content=data, media_type=content_type,
                        headers={"x-aithernet-sha256": sha})

    @app.get("/v1/portal/downloads/{release_id}/{name}")
    def portal_download(release_id: str, name: str, request: Request,
                        principal=Depends(principal_only)) -> Response:
        # Customer browser: authenticated session + membership + accepted required policies.
        try:
            data, content_type, sha = _svc(request).download_artifact_customer(
                principal, release_id=release_id, name=name)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        return Response(content=data, media_type=content_type,
                        headers={"x-aithernet-sha256": sha})

    @app.get("/v1/portal/releases/{release_id}/bundle")
    def portal_bundle_meta(release_id: str, request: Request,
                           principal=Depends(principal_only)) -> dict:
        # Display metadata for the Ubuntu delivery ZIP: stable filename, SHA-256, size, contained
        # file digests. Same authorization as the ZIP download itself.
        try:
            b = _svc(request).download_release_bundle_customer(principal, release_id=release_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        return {"filename": b["filename"], "sha256": b["sha256"],
                "byte_size": b["byte_size"], "files": b["files"]}

    @app.get("/v1/portal/releases/{release_id}/bundle.zip")
    def portal_bundle_zip(release_id: str, request: Request,
                          principal=Depends(principal_only)) -> Response:
        # One authenticated download: the server-assembled Ubuntu amd64 ZIP of the exact stored
        # bytes. Anonymous -> 401 (principal_only); unauthorized -> 403 (not_a_customer).
        try:
            b = _svc(request).download_release_bundle_customer(principal, release_id=release_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        return Response(content=b["zip"], media_type="application/zip", headers={
            "content-disposition": f'attachment; filename="{b["filename"]}"',
            "x-aithernet-sha256": b["sha256"]})

    @app.get("/v1/node/downloads/{release_id}/{name}")
    async def node_download(release_id: str, name: str, request: Request) -> Response:
        # Enrolled node update: signed node authentication before the bytes are served.
        await node_auth(request, f"/v1/node/downloads/{release_id}/{name}")
        try:
            data, content_type, sha = _svc(request).download_artifact_node(
                release_id=release_id, name=name)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc
        return Response(content=data, media_type=content_type,
                        headers={"x-aithernet-sha256": sha})


def _register_support(app, _svc, principal_only, csrf_guarded, _handle):
    @app.post("/v1/support/cases")
    def create_case(payload: dict, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).create_support_case(
                principal, tenant_id=payload["tenant_id"], subject=payload.get("subject", ""),
                category=payload.get("category", "general"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/support/cases")
    def list_cases(request: Request, principal=Depends(principal_only),
                   tenant_id: str | None = None) -> list:
        try:
            return _svc(request).list_support_cases(principal, tenant_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/support/cases/{case_id}")
    def get_case(case_id: str, request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).get_support_case(principal, case_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/support/cases/{case_id}/messages")
    def add_message(case_id: str, payload: dict, request: Request,
                    principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).add_support_message(
                principal, case_id=case_id, body=payload.get("body", ""))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/support/cases/{case_id}/bundle")
    async def upload_bundle(case_id: str, request: Request,
                            principal=Depends(csrf_guarded)) -> dict:
        data = await request.body()
        try:
            return _svc(request).upload_support_bundle(principal, case_id=case_id, data=data)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/support/cases/{case_id}/resolve")
    def resolve_case(case_id: str, payload: dict, request: Request,
                     principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).resolve_support_case(
                principal, case_id=case_id, status=payload.get("status", "resolved"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc


def _register_admin(app, _svc, principal_only, csrf_guarded, current, config, _handle):
    @app.get("/v1/admin/audit")
    def audit(request: Request, principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).list_audit(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/early-access")
    def early_access_list(request: Request, principal=Depends(principal_only),
                          status: str | None = None) -> dict:
        try:
            return _svc(request).list_early_access_requests(principal, status=status)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/early-access/{request_id}/approve")
    def early_access_approve(request_id: str, payload: dict, request: Request,
                             principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).approve_early_access_request(
                principal, request_id=request_id, role=payload.get("role"),
                tenant_name=payload.get("tenant_name"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/early-access/{request_id}/reject")
    def early_access_reject(request_id: str, request: Request,
                            principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).reject_early_access_request(principal, request_id=request_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/early-access/{request_id}/waitlist")
    def early_access_waitlist(request_id: str, request: Request,
                              principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).waitlist_early_access_request(principal, request_id=request_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/early-access/{request_id}/resend")
    def early_access_resend(request_id: str, request: Request,
                            principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).resend_early_access_invitation(principal, request_id=request_id)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/platform-grants")
    def grant(payload: dict, request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).grant_platform_role(
                principal, payload["user_id"], payload["role"])
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/email-preview")
    def email_preview(request: Request, principal=Depends(principal_only)) -> dict:
        svc = _svc(request)
        svc.require_platform_admin(principal)
        # Dev-mode + platform-admin ONLY. The development sink is the operator's local mailbox, so
        # exposing the dev invitation/reset link here is the intended retrieval path. In production
        # (SMTP) there is no sink, so this returns an empty list and `enabled: false`.
        enabled = svc.config.email.provider == "development"
        messages = getattr(svc.email, "messages", []) if enabled else []
        out = []
        for m in messages:
            item = m.preview()
            link = _extract_link(getattr(m, "body", ""))
            if link:
                item["link"] = link
            out.append(item)
        return {"enabled": enabled, "messages": out}

    @app.get("/v1/admin/users")
    def admin_users(request: Request, principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).list_users(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/drive-status")
    def drive_status(request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).drive_status(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    # -- research / owner-archive admin (platform-admin only, enforced in the service) -----

    @app.get("/v1/admin/research/status")
    def research_status(request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).research_admin_status(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/research/packages")
    def research_packages(request: Request, principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).research_list_packages(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/research/quarantine")
    def research_quarantine(request: Request, principal=Depends(principal_only)) -> list:
        try:
            return _svc(request).research_list_quarantine(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/research/jobs")
    def research_jobs(request: Request, principal=Depends(principal_only),
                      status: str | None = None) -> list:
        try:
            return _svc(request).research_list_jobs(principal, status=status)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/research/process-jobs")
    @app.post("/v1/admin/research/archive/process-jobs")
    def research_process_jobs(request: Request, principal=Depends(csrf_guarded)) -> dict:
        # Manual retry/testing only — steady-state archiving runs automatically. Both paths are
        # equivalent (the second matches the documented /archive/ route shape).
        try:
            return _svc(request).process_archive_jobs(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/research/drive")
    def owner_drive_status(request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).owner_drive_status(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/research/drive/oauth/start")
    def owner_drive_oauth_start(payload: dict, request: Request,
                                principal=Depends(csrf_guarded)) -> dict:
        # Admin/owner-only (CSRF-guarded). Mints a state nonce bound to this admin and returns the
        # Google authorization URL. The browser navigates to it; no secret is ever returned.
        try:
            return _svc(request).start_owner_drive_oauth(
                principal, account_label=payload.get("account_label"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/research/drive/oauth/callback")
    def owner_drive_oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        session: str | None = Cookie(default=None, alias=config.sessions.cookie_name),
    ) -> Response:
        # Google redirects the browser here (a top-level GET, so no CSRF header is possible — the
        # OAuth `state` nonce IS the CSRF defence, validated server-side against the admin's own
        # session/user). Always redirect back to the admin Research page with a bounded, non-secret
        # result; never render a raw stack trace or leak a code/token. Cloudflare Access protects
        # this admin origin and the admin's browser carries its session cookie through the redirect.
        from urllib.parse import urlparse

        from fastapi.responses import RedirectResponse

        from services.control_plane.errors import ControlPlaneError as _CPE

        # Redirect back to the admin SPA. Prefer a real configured admin origin; otherwise derive
        # it from the request's own Host (the admin origin the browser is on) so the redirect lands
        # correctly even when AITHERNET_ADMIN_BASE_URL is unset. TLS terminates at the edge, so a
        # non-loopback host is https (X-Forwarded-Proto from the loopback origin is not trusted).
        configured = (config.urls.admin or "").rstrip("/")
        cu = urlparse(configured)
        cu_host = (cu.hostname or "").lower()
        cu_loopback = cu_host in ("127.0.0.1", "localhost", "::1") or cu_host.endswith(".local")
        if configured and cu.scheme in ("http", "https") and not cu_loopback:
            base = configured
        else:
            req_host = request.headers.get("host")
            if req_host:
                low = req_host.split(":")[0].lower()
                scheme = "http" if low in ("127.0.0.1", "localhost", "::1") else "https"
                base = f"{scheme}://{req_host}"
            else:
                base = configured
        target = f"{base.rstrip('/')}/admin/research"

        def _back(result: str, reason: str | None = None) -> Response:
            q = f"drive={result}" + (f"&reason={reason}" if reason else "")
            return RedirectResponse(url=f"{target}?{q}", status_code=303)

        if error:
            return _back("error", "consent_denied")
        if not code or not state:
            return _back("error", "invalid_response")
        result = _svc(request).validate_session(session)
        if result is None:
            return _back("error", "session_expired")
        principal = result[0]
        try:
            _svc(request).complete_owner_drive_oauth(principal, code=code, state=state)
        except _CPE as exc:
            return _back("error", exc.code[:48])
        return _back("connected")

    @app.post("/v1/admin/research/drive/connect")
    def owner_drive_connect(payload: dict, request: Request,
                            principal=Depends(csrf_guarded)) -> dict:
        # ADVANCED fallback: store an operator-provided refresh token server-side. The normal path
        # is the OAuth flow above; the token is never returned or shown.
        try:
            return _svc(request).connect_owner_drive(
                principal, refresh_token=payload.get("refresh_token", ""),
                root_folder_id=payload.get("root_folder_id"),
                account_label=payload.get("account_label"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/research/drive/disconnect")
    def owner_drive_disconnect(request: Request, principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).disconnect_owner_drive(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/research/capabilities/revoke")
    def research_revoke_capability(payload: dict, request: Request,
                                   principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).revoke_capability(
                principal, tenant_id=payload.get("tenant_id", ""),
                node_id=payload.get("node_id", ""),
                reason=payload.get("reason", "admin_revoked"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.get("/v1/admin/research/policy")
    def research_get_policy(request: Request, principal=Depends(principal_only)) -> dict:
        try:
            return _svc(request).get_collection_policy(principal)
        except ControlPlaneError as exc:
            raise _handle(exc) from exc

    @app.post("/v1/admin/research/policy")
    def research_set_policy(payload: dict, request: Request,
                            principal=Depends(csrf_guarded)) -> dict:
        try:
            return _svc(request).set_collection_policy(
                principal, tenant_id=payload.get("tenant_id", "*"),
                collection_allowed=payload.get("collection_allowed"),
                auto_approve_beta=payload.get("auto_approve_beta"),
                max_package_bytes=payload.get("max_package_bytes"),
                retention_days=payload.get("retention_days"))
        except ControlPlaneError as exc:
            raise _handle(exc) from exc


# -- helpers -------------------------------------------------------------------------------------


def _principal_dep(current):
    def dep(auth: tuple = Depends(current)) -> Principal:
        return auth[0]
    return dep


def _extract_link(body: str) -> str | None:
    """Extract the first http(s) link from a development email body (dev sink + admin only)."""
    import re

    m = re.search(r"https?://\S+", body or "")
    return m.group(0) if m else None


def _set_session_cookies(response: Response, config: HostedConfig, session: dict) -> None:
    response.set_cookie(
        key=config.sessions.cookie_name, value=session["session_token"],
        httponly=True, secure=config.sessions.secure, samesite=config.sessions.same_site,
        max_age=config.sessions.ttl_seconds, path="/",
    )
    # CSRF cookie is readable by the SPA (double-submit) — it is not the session secret.
    response.set_cookie(
        key=config.sessions.csrf_cookie_name, value=session["csrf_token"],
        httponly=False, secure=config.sessions.secure, samesite=config.sessions.same_site,
        max_age=config.sessions.ttl_seconds, path="/",
    )


_ = (AuthError,)  # referenced for parity with the node API error surface
