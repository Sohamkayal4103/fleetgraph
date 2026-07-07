"""Central TLS CA-bundle resolution for HTTPS clients.

Packaged installs under ``/opt/aithernet`` (built with ``pip install --target``) can ship the
``certifi`` module without its ``cacert.pem`` data file. ``httpx`` then calls
``ssl.create_default_context(cafile=certifi.where())`` and raises ``FileNotFoundError`` at client
construction — breaking MCP diagnostics, GNU Radio context refresh and HTTPS provider calls.

Rather than disable verification (never), we resolve a REAL CA bundle — the Ubuntu system trust
store first, then certifi — and expose it for ``httpx`` (``verify_option``) and for the standard
``ssl`` env vars (``ensure_ssl_env``). TLS verification is always on.
"""

from __future__ import annotations

import os

#: System CA bundles, in priority order (Ubuntu/Debian first, then RHEL, then BSD/macOS).
_SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
)


def ca_bundle_path() -> str | None:
    """Return a path to a usable CA bundle (system store first, then certifi), or ``None`` when
    none can be found (callers then fall back to httpx's own default — never to no verification)."""
    for path in _SYSTEM_BUNDLES:
        if os.path.isfile(path):
            return path
    try:
        import certifi
        where = certifi.where()
        if os.path.isfile(where):
            return where
    except Exception:  # noqa: BLE001 — certifi missing or its data file absent (the bug we fix)
        pass
    return None


def verify_option() -> str | bool:
    """The value to pass as ``httpx`` ``verify=``: a resolved CA bundle path, else ``True`` (httpx
    default). Verification is never disabled."""
    return ca_bundle_path() or True


def ensure_ssl_env() -> str | None:
    """If ``SSL_CERT_FILE`` is unset or points at a missing file, set it (+ ``REQUESTS_CA_BUNDLE``)
    to a resolved CA bundle so ``ssl.create_default_context()`` never raises ``FileNotFoundError``
    on a packaged install. Returns the resolved bundle path (or ``None``). Idempotent."""
    current = os.environ.get("SSL_CERT_FILE")
    if current and os.path.isfile(current):
        return current
    bundle = ca_bundle_path()
    if bundle:
        os.environ["SSL_CERT_FILE"] = bundle
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
    return bundle
