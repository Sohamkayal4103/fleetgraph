"""Setup-journal reconciliation (beta.8 phase 8).

A single reconcile() derives real status from the live system so the journal stops going stale after
external commands (sdr install / components install / agents configure) complete.
"""

from __future__ import annotations

from aithernet.provisioning import state as S
from aithernet.provisioning import wizard


def test_reconcile_marks_configured_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    from aithernet.agents import providers as prov
    prov.configure("coordinator", "gemini_api", model="gemini-2.0-flash",
                   api_key_ref="GEMINI_API_KEY", state_root=tmp_path)
    st = S.SetupState(created_at="t0")
    st.record("coordinator_provider", S.DEFERRED, "stale", now="t0")  # pretend stale
    wizard.reconcile(st, state_root=tmp_path)
    assert st.status_of("coordinator_provider") in (S.CONFIGURED, S.VERIFIED)
    assert st.coordinator_provider == "gemini_api"


def test_reconcile_marks_none_strategy_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    st = S.SetupState(created_at="t0", sdr_strategy="none")
    st.record("sdr_strategy", S.DEFERRED, "stale", now="t0")
    wizard.reconcile(st, state_root=tmp_path)
    assert st.status_of("sdr_strategy") == S.SKIPPED


def test_reconcile_verifies_gnuradio_when_importable(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    # Simulate gnuradio being importable (reconcile checks importlib.util.find_spec).
    import importlib.util as _ilu
    monkeypatch.setattr(_ilu, "find_spec",
                        lambda name, *a, **k: object() if name == "gnuradio" else None)
    st = S.SetupState(created_at="t0", sdr_strategy="recommended")
    st.record("sdr_strategy", S.DEFERRED, "stale", now="t0")
    wizard.reconcile(st, state_root=tmp_path)
    assert st.status_of("sdr_strategy") == S.VERIFIED


def test_reconcile_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    from aithernet.agents import providers as prov
    prov.configure("coding", "codex_cli", executable="codex", state_root=tmp_path)
    st = S.SetupState(created_at="t0")
    wizard.reconcile(st, state_root=tmp_path)
    first = st.status_of("coding_provider")
    wizard.reconcile(st, state_root=tmp_path)
    assert st.status_of("coding_provider") == first
    # the reconciled state persists to disk
    reloaded = S.load_state(tmp_path)
    assert reloaded.status_of("coding_provider") == first


def test_disabled_provider_reconciled_done(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    st = S.SetupState(created_at="t0")
    wizard.reconcile(st, state_root=tmp_path)  # nothing configured -> coordinator disabled
    assert st.status_of("coordinator_provider") == S.DONE
