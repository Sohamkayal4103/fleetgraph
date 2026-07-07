"""TLS CA-bundle resolution (beta.8 phase 2).

Packaged installs may lack certifi's cacert.pem; we resolve a real CA bundle and never disable
verification. Covers httpx client creation + the ssl env shim.
"""

from __future__ import annotations

import os

import httpx

from aithernet import tls


def test_ca_bundle_path_is_existing_or_none():
    p = tls.ca_bundle_path()
    assert p is None or os.path.isfile(p)


def test_verify_option_never_disables_verification():
    v = tls.verify_option()
    assert v is not False                       # never disables TLS verification
    assert v is True or (isinstance(v, str) and os.path.isfile(v))


def test_local_httpx_client_creation_works():
    # The exact failure mode (client construction crashing on a missing CA file) must not occur.
    client = httpx.Client(verify=tls.verify_option())
    client.close()


def test_async_https_client_creation_works():
    client = httpx.AsyncClient(verify=tls.verify_option())
    # constructing is the crash point; closing the (unused) client is a coroutine we can ignore
    import asyncio
    asyncio.run(client.aclose())


def test_ensure_ssl_env_preserves_valid_existing(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    assert tls.ensure_ssl_env() == str(ca)


def test_ensure_ssl_env_replaces_missing_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "does-not-exist.pem"))
    resolved = tls.ensure_ssl_env()
    if resolved is not None:  # a real bundle exists on this host -> it must replace the broken path
        assert os.path.isfile(os.environ["SSL_CERT_FILE"])
        assert os.environ["SSL_CERT_FILE"] == resolved


def test_ensure_ssl_env_sets_when_unset(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    resolved = tls.ensure_ssl_env()
    if resolved is not None:
        assert os.path.isfile(resolved)
        assert os.environ["SSL_CERT_FILE"] == resolved
