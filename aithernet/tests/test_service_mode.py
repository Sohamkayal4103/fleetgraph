"""Personal-workstation service mode (beta.5 Area 3).

The packaged setup supports a real same-user (systemd --user) service so the node inherits the
desktop user's Gemini/Codex CLI authentication, with an optional 0600 env file for appliance-style
API keys; setup and doctor report the actual execution identity.
"""

from __future__ import annotations

from aithernet.provisioning import services


def test_user_unit_runs_as_user_with_optional_env_file():
    unit = services.user_unit_text()
    assert "WantedBy=default.target" in unit                 # user scope
    assert "EnvironmentFile=-%h/.config/aithernet/aithernet.env" in unit  # optional API-key file
    assert "User=" not in unit                                # not a fixed system account
    assert "aithernet start" in unit


def test_installed_scope_detects_user_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Isolate SYSTEM-unit detection from whatever is installed on this build/dev host (a real
    # /lib/systemd/system/aithernet-node.service must not influence the result).
    monkeypatch.setattr(services, "system_unit_dirs", lambda: [tmp_path / "no-system-units"])
    assert services.installed_scope() is None
    services.install_user_unit()
    assert services.installed_scope() == "user"
    assert services.systemctl_base("user") == ["systemctl", "--user"]
    assert services.systemctl_base("system") == ["systemctl"]


def test_system_unit_is_appliance_under_account():
    unit = services.system_unit_text()
    assert "User=aithernet" in unit
    assert "WantedBy=multi-user.target" in unit


def test_doctor_reports_execution_identity(monkeypatch, tmp_path):
    # A run with setup state present should surface the execution identity.
    from aithernet.provisioning import state as sstate
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    st = sstate.SetupState()
    st.deployment_mode = "standalone"
    st.service_mode = "systemd-user"
    st.identity_model = "workstation"
    sstate.save_state(st, tmp_path)

    from aithernet import doctor
    rep = doctor.DoctorReport()
    doctor._check_setup_state(rep)
    ident = next((c for c in rep.checks if c.name == "execution_identity"), None)
    assert ident is not None
    assert "identity model workstation" in ident.detail
