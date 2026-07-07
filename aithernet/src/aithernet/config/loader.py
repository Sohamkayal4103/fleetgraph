"""Load node configuration from YAML with environment-variable overrides.

Before any ``env:VAR_NAME`` reference or ``AITHERNET_*`` override is resolved, the
repository-root ``.env`` is loaded into the process environment with ``override=False`` so
explicitly exported shell variables always win over ``.env`` values. This means ordinary
local startup no longer requires ``set -a; source .env; set +a``. ``.env`` contents are
never logged.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from aithernet.config.settings import NodeConfig

#: Default location of the node configuration, relative to the repository root.
DEFAULT_CONFIG_PATH = Path("configs/node.yaml")


def canonical_node_config_path() -> Path:
    """The installed node's canonical config: ``<state_root>/config/node.yaml`` where state_root is
    ``AITHERNET_STATE_ROOT`` else ``~/.local/share/aithernet``. This is the file ``aithernet setup``
    writes and the systemd unit pins; ``aithernet start`` prefers it over the dev-tree
    ``configs/node.yaml`` so a customer never silently starts on placeholder defaults."""
    root = os.environ.get("AITHERNET_STATE_ROOT") or str(Path.home() / ".local/share/aithernet")
    return Path(root) / "config" / "node.yaml"

#: Repository root (……/src/aithernet/config/loader.py -> repo root).
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The single ``.env`` file auto-loaded before config resolution. Deliberately a fixed,
#: known path — we never walk parent directories looking for unrelated ``.env`` secrets.
PROJECT_ENV_PATH = PROJECT_ROOT / ".env"

#: Environment variables of the form ``AITHERNET_<FIELD>`` override config fields.
_ENV_PREFIX = "AITHERNET_"

#: Fields whose environment overrides must be coerced from string.
_INT_FIELDS = {"port"}

#: Fields whose environment overrides are comma-separated lists.
_LIST_FIELDS = {"cors_allow_origins"}

#: Prefix marking a value that should be resolved from the environment, e.g.
#: ``model: env:AITHERNET_COORDINATOR_MODEL``.
_ENV_REF_PREFIX = "env:"


def load_dotenv_file(env_path: str | os.PathLike[str] | None = None) -> bool:
    """Load a ``.env`` file into ``os.environ`` with ``override=False``.

    Returns ``True`` if a file was found and loaded. A missing file is not an error
    (returns ``False``). ``override=False`` guarantees explicitly exported shell variables
    are never clobbered by ``.env``. Values are never logged. Only the given path (default
    :data:`PROJECT_ENV_PATH`) is consulted — no parent-directory search.
    """
    path = Path(env_path) if env_path is not None else PROJECT_ENV_PATH
    if not path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv(dotenv_path=path, override=False)


def _resolve_env_refs(value: object) -> object:
    """Recursively resolve ``env:VAR_NAME`` references anywhere in the config tree.

    An ``env:`` string is replaced by the environment variable's value, or ``None`` if
    the variable is unset (so optional config simply stays absent). Non-string values
    and plain strings pass through unchanged.
    """
    if isinstance(value, str) and value.startswith(_ENV_REF_PREFIX):
        return os.environ.get(value[len(_ENV_REF_PREFIX) :])
    if isinstance(value, dict):
        return {key: _resolve_env_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs(item) for item in value]
    return value


def _apply_env_overrides(values: dict) -> dict:
    """Overlay ``AITHERNET_*`` environment variables onto loaded config values."""
    merged = dict(values)
    for field in NodeConfig.model_fields:
        env_key = f"{_ENV_PREFIX}{field.upper()}"
        if env_key in os.environ:
            raw = os.environ[env_key]
            if field in _INT_FIELDS:
                merged[field] = int(raw)
            elif field in _LIST_FIELDS:
                merged[field] = [item.strip() for item in raw.split(",") if item.strip()]
            else:
                merged[field] = raw
    return merged


def _autowire_managed_mcp(config: NodeConfig) -> None:
    """Wire the installed managed RF-MCP component into the GNU Radio MCP launch.

    Explicit configuration always wins: this only fills an *unset* ``gnuradio_mcp.command``
    (e.g. when ``AITHERNET_GNURADIO_MCP_COMMAND`` is not exported). When the command is unset
    but the managed ``rf-mcp`` component is installed and intact, the launch is taken from the
    component lock's ``runtime_command`` and the install prefix — so a clean customer install
    runs RF missions with no developer checkout and no hand-set environment variables. If the
    component is absent or broken, the command stays ``None`` (unconfigured), and any tool call
    fails with a clear error rather than faking GNU Radio behavior.
    """
    mcp = getattr(config, "gnuradio_mcp", None)
    if mcp is None or mcp.command:
        return
    try:
        from aithernet.components import installed_mcp_launch

        launch = installed_mcp_launch("rf-mcp")
    except Exception:  # noqa: BLE001 — never let component resolution break config load
        launch = None
    if launch is None:
        return
    # Replace the placeholder launch shape wholesale: the lock is the source of truth for
    # how to start the installed server (command + ordered args + cwd).
    mcp.command = launch.command
    mcp.args = list(launch.args)
    mcp.cwd = launch.cwd


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    env_file: str | os.PathLike[str] | None = None,
) -> NodeConfig:
    """Load and validate node configuration.

    The repository-root ``.env`` (or ``env_file`` when given, for tests) is loaded first
    with ``override=False``, so ``env:VAR_NAME`` references and ``AITHERNET_*`` overrides
    can resolve from it while exported shell variables still take precedence.

    Resolution order:
      1. Explicit ``path`` argument, if given.
      2. ``AITHERNET_CONFIG`` environment variable, if set.
      3. ``configs/node.yaml`` relative to the current working directory.

    A missing file is tolerated (model defaults plus a generated identity are used);
    a malformed file raises. Environment variables always take precedence.
    """
    load_dotenv_file(env_file)
    config_path = Path(path or os.environ.get("AITHERNET_CONFIG", DEFAULT_CONFIG_PATH))

    values: dict = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file {config_path} must contain a YAML mapping.")
        values = loaded

    values.setdefault("node_id", "00000000-0000-4000-8000-000000000001")
    values.setdefault("node_name", "aithernet-node")

    values = _resolve_env_refs(values)  # type: ignore[assignment]
    values = _apply_env_overrides(values)
    config = NodeConfig.model_validate(values)
    _autowire_managed_mcp(config)
    return config
