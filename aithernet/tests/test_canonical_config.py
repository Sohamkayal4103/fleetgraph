"""Canonical config/state-path contract (beta.8 blocker #3).

Setup must bind ONE canonical node.yaml with a real node id and absolute db/identity/workspace
paths under a single state root, the systemd user unit must pin AITHERNET_CONFIG to it, and the
runtime must load exactly that — never a placeholder node id, a cwd-relative database, or an
identity directory different from the one setup created.
"""

from __future__ import annotations

from pathlib import Path

from aithernet.config.loader import canonical_node_config_path, load_config
from aithernet.provisioning import services, state, wizard

_PLACEHOLDER = "00000000-0000-4000-8000-000000000001"


def _setup_to(root: Path, **opts):
    s = state.SetupState(created_at="t0", node_name=opts.get("node_name", "node-x"))
    wizard.run(s, state_root=root, opts=opts, only=["node_identity", "directories"])
    return s


def test_setup_writes_canonical_config_with_real_node_id(tmp_path):
    _setup_to(tmp_path, node_name="mp-wsl-beta8")
    cfg = load_config(tmp_path / "config" / "node.yaml")
    assert cfg.node_id and cfg.node_id != _PLACEHOLDER
    assert cfg.node_name == "mp-wsl-beta8"


def test_database_path_is_absolute_under_state_root(tmp_path):
    _setup_to(tmp_path)
    cfg = load_config(tmp_path / "config" / "node.yaml")
    assert cfg.database_url.startswith("sqlite:////")  # 4 slashes => absolute
    assert str((tmp_path / "db" / "aithernet.db").resolve()) in cfg.database_url


def test_identity_dir_is_where_setup_created_the_key(tmp_path):
    _setup_to(tmp_path)
    cfg = load_config(tmp_path / "config" / "node.yaml")
    ident = Path(cfg.agent_transport.identity.state_directory)
    assert ident == (tmp_path / "identity").resolve()
    # setup actually created the key there (the runtime will read THIS key, not a divergent one)
    assert (tmp_path / "identity").is_dir()
    assert any((tmp_path / "identity").iterdir())


def test_coding_workspace_is_absolute(tmp_path):
    _setup_to(tmp_path)
    cfg = load_config(tmp_path / "config" / "node.yaml")
    ws = cfg.coding_agent.workspace
    assert ws and Path(ws).is_absolute()
    assert ws == str((tmp_path / "coding").resolve())


def test_node_state_root_recorded(tmp_path):
    _setup_to(tmp_path)
    cfg = load_config(tmp_path / "config" / "node.yaml")
    assert cfg.node_state_root == str(tmp_path.resolve())


def test_node_id_stable_across_reruns(tmp_path):
    s1 = _setup_to(tmp_path, node_name="stable")
    first = load_config(tmp_path / "config" / "node.yaml").node_id
    # Re-run directories (idempotent) with the SAME persisted state -> same node id.
    wizard.run(s1, state_root=tmp_path, opts={}, only=["directories"], repair=True)
    second = load_config(tmp_path / "config" / "node.yaml").node_id
    assert first == second == s1.node_id


def test_canonical_write_preserves_provider_sections(tmp_path):
    # A provider section written first must survive the canonical bind (no clobber).
    from aithernet.agents import providers as prov
    prov.configure("coordinator", "gemini_cli", state_root=tmp_path)
    _setup_to(tmp_path)
    cfg = load_config(tmp_path / "config" / "node.yaml")
    assert cfg.coordinator.provider == "gemini_cli"     # preserved
    assert cfg.node_id != _PLACEHOLDER                  # and canonical fields added


def test_systemd_user_unit_pins_canonical_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgconfig"))
    s = state.SetupState(created_at="t0", node_name="svc", service_mode="systemd-user",
                         identity_model="workstation")
    wizard.run(s, state_root=tmp_path, opts={"service_mode": "systemd-user"},
               only=["service_install"])
    unit = (tmp_path / "xdgconfig" / "systemd" / "user" / services.UNIT_NAME).read_text()
    assert f"AITHERNET_CONFIG={tmp_path / 'config' / 'node.yaml'}" in unit
    assert f"AITHERNET_STATE_ROOT={tmp_path}" in unit
    assert "aithernet start" in unit


def test_canonical_path_helper_honors_state_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    assert canonical_node_config_path() == tmp_path / "config" / "node.yaml"
    monkeypatch.delenv("AITHERNET_STATE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    expected = tmp_path / "home" / ".local/share/aithernet/config/node.yaml"
    assert canonical_node_config_path() == expected


def test_no_placeholder_when_config_present(tmp_path):
    """Loading the canonical config must never yield the placeholder id."""
    _setup_to(tmp_path)
    cfg = load_config(tmp_path / "config" / "node.yaml")
    assert cfg.node_id != _PLACEHOLDER
