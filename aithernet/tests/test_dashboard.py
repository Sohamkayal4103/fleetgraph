"""Stage 8 backend additions: CORS for the browser dashboard + optional static mount.

These tests do NOT build or require the frontend. They exercise only the backend changes
that support it: CORS headers (so a cross-origin Vite dev server can call the API) and the
optional ``/dashboard`` static mount (served only when a built directory exists).
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.loader import load_config
from aithernet.config.settings import NodeConfig


def test_cors_headers_present_for_browser_origin(config: NodeConfig) -> None:
    app = create_app(config)  # default cors_allow_origins == ["*"]
    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://localhost:5173"})
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"


def test_cors_preflight_is_allowed(config: NodeConfig) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        response = client.options(
            "/missions",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"


def test_dashboard_not_mounted_when_absent(config: NodeConfig, tmp_path) -> None:
    # Point at a directory that does not exist: no mount, API unaffected.
    app = create_app(config, dashboard_dir=tmp_path / "does-not-exist")
    with TestClient(app) as client:
        assert client.get("/dashboard/").status_code == 404
        assert client.get("/health").status_code == 200  # API still works


def test_dashboard_served_when_built_dir_exists(config: NodeConfig, tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Aithernet</title>")
    app = create_app(config, dashboard_dir=dist)
    with TestClient(app) as client:
        response = client.get("/dashboard/")
        assert response.status_code == 200
        assert "Aithernet" in response.text
        # The API is still fully functional alongside the static mount.
        assert client.get("/node/status").status_code == 200


def test_cors_origins_env_override_is_split(tmp_path) -> None:
    cfg_path = tmp_path / "node.yaml"
    cfg_path.write_text("node_id: t\nnode_name: t\n")
    key = "AITHERNET_CORS_ALLOW_ORIGINS"
    previous = os.environ.get(key)
    os.environ[key] = "http://localhost:5173, http://127.0.0.1:5173"
    try:
        loaded = load_config(cfg_path, env_file=tmp_path / "absent.env")
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
    assert loaded.cors_allow_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
