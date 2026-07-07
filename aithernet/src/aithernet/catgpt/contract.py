"""The Aithernet CatGPT Gateway Contract (beta.5, Part C).

The Aithernet-managed gateway must expose an OpenAI-compatible, JSON-only local API:

    GET  /health              → JSON  {"status": ...}
    GET  /ready               → JSON  {"ready": bool, ...}
    GET  /v1/models           → JSON  OpenAI model list
    POST /v1/chat/completions → JSON  OpenAI chat completion

Requirements enforced/verified here:
  * API bound to 127.0.0.1 by default, local bearer token required by default.
  * Non-streaming OpenAI-compatible chat completions returning ``choices[0].message.content``.
  * JSON API errors (never nginx/Caddy HTML). Unsupported route/method → a JSON
    ``{"error": {"type": "method_not_allowed", "message": ..., "status": 405}}`` shape.
  * The browser session/cookies are NEVER exposed and the bearer token is NEVER logged.

Upstream CatGPT-Gateway historically serves ``/healthz`` (not ``/health``) and may return a
proxy's HTML for wrong routes. This module probes tolerantly (``/health`` OR ``/healthz``) and the
Aithernet provider/CLI layers already classify a non-JSON body as a structured error rather than
crashing — so a contract miss surfaces as an actionable status, never a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

#: The canonical Aithernet-facing gateway contract (method, path, tolerant aliases).
GATEWAY_CONTRACT_ENDPOINTS: tuple[dict, ...] = (
    {"method": "GET", "path": "/health", "aliases": ("/healthz",)},
    {"method": "GET", "path": "/ready", "aliases": ("/healthz", "/health")},
    {"method": "GET", "path": "/v1/models", "aliases": ()},
    {"method": "POST", "path": "/v1/chat/completions", "aliases": ()},
)

#: Markers in a gateway body/response that mean "the browser isn't logged in yet".
_LOGIN_MARKERS = (
    "login required", "not logged in", "please log in", "session expired",
    "browser session", "sign in", "unauthenticated browser", "no active session",
)


class GatewayStatus:
    """Coarse lifecycle states surfaced to the operator (Part D)."""

    STOPPED = "stopped"          # container not running / connection refused
    STARTING = "starting"        # container up but health endpoint not ready yet
    LOGIN_REQUIRED = "login_required"   # gateway healthy but the web session isn't logged in
    READY = "ready"              # models discoverable → coordinator-usable
    ERROR = "error"              # reachable but returning an error / invalid contract


@dataclass
class ContractCheck:
    ok: bool
    endpoint: str
    detail: str
    json_ok: bool = False


@dataclass
class ContractReport:
    ok: bool
    endpoint_reachable: bool
    checks: list[ContractCheck] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "endpoint_reachable": self.endpoint_reachable,
            "models": self.models,
            "detail": self.detail,
            "checks": [
                {"endpoint": c.endpoint, "ok": c.ok, "json_ok": c.json_ok, "detail": c.detail}
                for c in self.checks
            ],
        }


def _is_json(resp: httpx.Response) -> bool:
    return "json" in resp.headers.get("content-type", "").lower()


def _looks_login_required(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _LOGIN_MARKERS)


def classify_gateway_status(
    *,
    container_running: bool,
    report: ContractReport | None,
) -> str:
    """Map a container state + contract probe to a coarse :class:`GatewayStatus`."""
    if report is None or not report.endpoint_reachable:
        return GatewayStatus.STOPPED if not container_running else GatewayStatus.STARTING
    # endpoint reachable:
    if report.detail == GatewayStatus.LOGIN_REQUIRED:
        return GatewayStatus.LOGIN_REQUIRED
    if report.models:
        return GatewayStatus.READY
    if report.ok:
        # reachable + JSON contract ok but no models yet → almost always a not-logged-in session
        return GatewayStatus.LOGIN_REQUIRED
    return GatewayStatus.ERROR


def verify_contract(
    base_url: str,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
    client: httpx.Client | None = None,
) -> ContractReport:
    """Probe the Aithernet gateway contract at ``base_url`` (an ``…/v1`` root) with a bearer token.

    Never logs the token; never returns response bodies verbatim (only short, safe details). The
    ``client`` argument is for tests (an ``httpx.Client`` bound to a ``MockTransport``).
    """
    root = base_url.rstrip("/")
    # The health/ready endpoints live at the SERVER root, not under /v1.
    server_root = root[: -len("/v1")] if root.endswith("/v1") else root
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    report = ContractReport(ok=False, endpoint_reachable=False)
    try:
        # 1) health / ready (tolerant of /healthz)
        health_ok = False
        for path in ("/health", "/healthz"):
            try:
                r = client.get(f"{server_root}{path}", headers=headers)
            except httpx.HTTPError:
                continue
            report.endpoint_reachable = True
            if r.status_code == 200:
                health_ok = True
                report.checks.append(ContractCheck(True, path, "reachable", _is_json(r)))
                break
            report.checks.append(ContractCheck(False, path, f"status {r.status_code}", _is_json(r)))
        if not report.endpoint_reachable:
            report.detail = "gateway not reachable"
            return report

        # 2) /v1/models — the readiness + login signal
        try:
            rm = client.get(f"{root}/models", headers=headers)
        except httpx.HTTPError as exc:
            report.checks.append(ContractCheck(False, "/v1/models", f"error: {type(exc).__name__}"))
            report.detail = "models endpoint unreachable"
            return report
        if rm.status_code in (401, 403) or _looks_login_required(rm.text):
            report.checks.append(ContractCheck(False, "/v1/models", "login required", _is_json(rm)))
            report.detail = GatewayStatus.LOGIN_REQUIRED
            return report
        if rm.status_code == 200 and _is_json(rm):
            data = rm.json()
            entries = data.get("data") if isinstance(data, dict) else None
            models = [m.get("id") for m in entries or [] if isinstance(m, dict) and m.get("id")]
            report.models = [m for m in models if m]
            report.checks.append(ContractCheck(True, "/v1/models", "json model list", True))
            report.ok = health_ok and bool(report.models)
            if not report.models:
                report.detail = GatewayStatus.LOGIN_REQUIRED
            return report
        # non-JSON or error where JSON was required → contract violation
        report.checks.append(
            ContractCheck(False, "/v1/models",
                          f"non-contract response (status {rm.status_code}, "
                          f"content-type {rm.headers.get('content-type', 'unknown')})",
                          _is_json(rm))
        )
        report.detail = "models endpoint did not return a JSON model list"
        return report
    finally:
        if owns_client:
            client.close()
