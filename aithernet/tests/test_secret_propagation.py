"""beta.10 Defect 1: generic provider-secret persistence + propagation to the running service.

These prove the secret store accepts ANY UPPER_SNAKE_CASE name (not just provider-specific ones),
that presence is reported from the credential file / running service (not just the CLI's transient
environment — the beta.9 defect), and that the new `agents secrets list/check/remove` commands show
names + availability only, never values.
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from aithernet.agents import providers as prov
from aithernet.cli import app
from aithernet.provisioning import services


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Keep the credential file inside the test sandbox; never touch the real ~/.config.
    env_file = tmp_path / "aithernet.env"
    monkeypatch.setattr(services, "system_unit_dirs", lambda: [])  # ignore dev-host system unit
    monkeypatch.setattr(prov, "secret_env_file", lambda: env_file)
    monkeypatch.setenv("AITHERNET_SECRET_ENV_FILE_TEST", str(env_file))
    return env_file


def test_arbitrary_secret_name_persists_0600(_isolate):
    # An arbitrary (non-provider-specific) name is accepted and stored 0600.
    path = prov.set_secret("ZAI_API_KEY", "secret-value-123")
    assert path == _isolate
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert "ZAI_API_KEY" in prov.list_secret_names()
    # The value is NOT discoverable through the name listing.
    assert "secret-value-123" not in " ".join(prov.list_secret_names())


def test_value_never_in_listing_and_removable(_isolate):
    prov.set_secret("GENERIC_TEST_KEY", "v1")
    prov.set_secret("OTHER_KEY", "v2")
    assert prov.list_secret_names() == ["GENERIC_TEST_KEY", "OTHER_KEY"]
    assert prov.remove_secret("GENERIC_TEST_KEY") is True
    assert prov.list_secret_names() == ["OTHER_KEY"]
    assert prov.remove_secret("GENERIC_TEST_KEY") is False  # idempotent


def test_credential_available_from_file_not_just_process_env(_isolate, monkeypatch):
    # The beta.9 bug: api_key_present only checked os.environ of the CLI process. A stored secret
    # must read as available even when this process's environment does not carry it.
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    assert prov._credential_available("ZAI_API_KEY") is False
    prov.set_secret("ZAI_API_KEY", "k")
    assert prov._credential_available("ZAI_API_KEY") is True


def test_service_sees_env_reads_process_environ(monkeypatch):
    # Launch a child holding the probe var in its START environment (what /proc/<pid>/environ
    # reflects), point the "service MainPID" at it, and confirm presence detection (not the value).
    import subprocess
    import time

    env = {**os.environ, "AITH_PRESENCE_PROBE": "yes"}
    env.pop("AITH_DEFINITELY_ABSENT_VAR", None)
    child = subprocess.Popen(["sleep", "5"], env=env)
    try:
        time.sleep(0.1)
        monkeypatch.setattr(services, "service_main_pid", lambda: child.pid)
        assert services.service_sees_env("AITH_PRESENCE_PROBE") is True
        assert services.service_sees_env("AITH_DEFINITELY_ABSENT_VAR") is False
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_service_sees_env_none_when_not_running(monkeypatch):
    monkeypatch.setattr(services, "service_main_pid", lambda: None)
    assert services.service_sees_env("ANYTHING") is None


def test_propagate_no_managed_service(monkeypatch):
    monkeypatch.setattr(services, "installed_scope", lambda: None)
    rec = services.propagate_secret_to_service("ZAI_API_KEY")
    assert rec["scope"] is None and rec["restarted"] is False
    assert "foreground" in rec["detail"]


def test_propagate_system_scope_defers_to_sudo(monkeypatch):
    monkeypatch.setattr(services, "installed_scope", lambda: "system")
    rec = services.propagate_secret_to_service("ZAI_API_KEY")
    assert rec["scope"] == "system" and rec["restarted"] is False
    assert "sudo" in rec["detail"]


def test_secrets_list_and_check_cli_show_no_values(_isolate, monkeypatch):
    prov.set_secret("ZAI_API_KEY", "topsecret")
    monkeypatch.setattr(services, "service_sees_env", lambda name: None)
    runner = CliRunner()
    out = runner.invoke(app, ["agents", "secrets", "list"])
    assert out.exit_code == 0, out.output
    assert "ZAI_API_KEY" in out.output and "topsecret" not in out.output
    chk = runner.invoke(app, ["agents", "secrets", "check", "ZAI_API_KEY"])
    assert chk.exit_code == 0
    assert '"stored": true' in chk.output and "topsecret" not in chk.output


def test_secrets_remove_cli(_isolate, monkeypatch):
    prov.set_secret("ZAI_API_KEY", "x")
    monkeypatch.setattr(services, "installed_scope", lambda: None)
    runner = CliRunner()
    out = runner.invoke(app, ["agents", "secrets", "remove", "ZAI_API_KEY"])
    assert out.exit_code == 0
    assert "Removed" in out.output
    assert "ZAI_API_KEY" not in prov.list_secret_names()
