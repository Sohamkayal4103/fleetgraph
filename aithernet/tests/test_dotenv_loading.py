"""Part F: automatic ``.env`` loading by the config loader.

The loader reads a repository-root ``.env`` before resolving ``env:VAR`` references, with
``override=False`` so exported shell variables win. These tests use a temporary ``.env``
(never the real one) and clean up ``os.environ`` so they do not pollute the session.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.loader import load_config

_VARS = [
    "AITHERNET_COORDINATOR_MODEL",
    "AITHERNET_COORDINATOR_BASE_URL",
    "AITHERNET_COORDINATOR_API_KEY",
]


@pytest.fixture(autouse=True)
def _clean_env() -> Iterator[None]:
    """Snapshot and restore the env vars these tests touch (dotenv mutates os.environ)."""
    saved = {key: os.environ.get(key) for key in _VARS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_node_yaml(path) -> None:
    path.write_text(
        "node_id: dotenv-test\n"
        "node_name: dotenv-test\n"
        "coordinator:\n"
        "  provider: openai_compatible\n"
        "  model: env:AITHERNET_COORDINATOR_MODEL\n"
        "  base_url: env:AITHERNET_COORDINATOR_BASE_URL\n"
        "  api_key: env:AITHERNET_COORDINATOR_API_KEY\n"
    )


def test_dotenv_values_are_loaded(tmp_path) -> None:
    cfg = tmp_path / "node.yaml"
    _write_node_yaml(cfg)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AITHERNET_COORDINATOR_MODEL=qwen2.5:7b\n"
        "AITHERNET_COORDINATOR_BASE_URL=http://127.0.0.1:11434/v1\n"
        "AITHERNET_COORDINATOR_API_KEY=dotenv-secret-value\n"
    )

    loaded = load_config(cfg, env_file=env_file)

    assert loaded.coordinator.model == "qwen2.5:7b"
    assert loaded.coordinator.base_url == "http://127.0.0.1:11434/v1"
    assert loaded.coordinator.api_key == "dotenv-secret-value"


def test_exported_shell_var_overrides_dotenv(tmp_path) -> None:
    cfg = tmp_path / "node.yaml"
    _write_node_yaml(cfg)
    env_file = tmp_path / ".env"
    env_file.write_text("AITHERNET_COORDINATOR_MODEL=from-dotenv\n")

    os.environ["AITHERNET_COORDINATOR_MODEL"] = "from-shell"
    loaded = load_config(cfg, env_file=env_file)

    assert loaded.coordinator.model == "from-shell"  # override=False: shell wins


def test_missing_dotenv_is_harmless(tmp_path) -> None:
    cfg = tmp_path / "node.yaml"
    _write_node_yaml(cfg)
    loaded = load_config(cfg, env_file=tmp_path / "does-not-exist.env")
    # No .env, env unset -> env: refs resolve to None (fields stay unset/None).
    assert loaded.coordinator.model is None
    assert loaded.node_id == "dotenv-test"


def test_dotenv_secret_not_exposed_in_status(tmp_path) -> None:
    cfg = tmp_path / "node.yaml"
    db = tmp_path / "dotenv.db"
    cfg.write_text(
        "node_id: dotenv-status\n"
        "node_name: dotenv-status\n"
        f"database_url: sqlite:///{db}\n"
        "coordinator:\n"
        "  provider: openai_compatible\n"
        "  model: env:AITHERNET_COORDINATOR_MODEL\n"
        "  api_key: env:AITHERNET_COORDINATOR_API_KEY\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AITHERNET_COORDINATOR_MODEL=qwen2.5:7b\n"
        "AITHERNET_COORDINATOR_API_KEY=super-secret-dotenv-key-xyz\n"
    )
    config = load_config(cfg, env_file=env_file)

    with TestClient(create_app(config)) as client:
        coord = client.get("/coordinator/status")
        assert coord.status_code == 200
        # The secret API key must never appear in a status response.
        assert "super-secret-dotenv-key-xyz" not in coord.text
        assert coord.json()["api_key_configured"] is True  # presence reported, not value
        assert "super-secret-dotenv-key-xyz" not in client.get("/node/status").text
