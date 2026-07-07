"""beta.8 — data-collection / Google Drive pipeline fix.

Covers: automatic owner_full recording on mission completion; sanitized records that carry
mission/run/tool-accountability/coding-task/MCP metadata and NEVER secrets; the collect/sync
packaging + auth-gated upload; the honest OAuth-client-missing degradation; the status/verify UX;
and that no beta.10 (future-version) labels leak into the CLI help.
"""
from __future__ import annotations

import contextlib
import json
import types

import pytest
from typer.testing import CliRunner

from aithernet.cli import app, data_app
from aithernet.data import research_recorder as rr
from aithernet.data import research_sync as rs
from aithernet.data.research_spool import ResearchSpool

runner = CliRunner()


# -- isolation fixture: private spool + drive-state dir + node config ---------------------------
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AITHERNET_DRIVE_STATE_DIR", str(tmp_path / "drive"))
    cfg = tmp_path / "node.yaml"
    cfg.write_text("data_platform:\n  collection:\n    data_collection_mode: owner_full\n"
                   "    research_consent: true\n    research_upload_mode: hosted_owner_archive\n"
                   "    research_auto_sync: false\n")
    monkeypatch.setenv("AITHERNET_CONFIG", str(cfg))
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path / "state"))
    return tmp_path


# -- fake runtime for the recorder -------------------------------------------------------------
def _runtime(*, mode="owner_full", events=None, mcp=None, mission=None, run=None,
             session_ok=False):
    coll = types.SimpleNamespace(data_collection_mode=mode, research_auto_sync=False)
    config = types.SimpleNamespace(
        data_platform=types.SimpleNamespace(collection=coll))

    class _Ev:
        def __init__(self, t, m, p):
            self.created_at = None
            self.event_type = t
            self.source = "mission_worker"
            self.message = m
            self.payload = p

    ev_rows = [_Ev(*e) for e in (events or [])]

    class RT:
        def __init__(self):
            self.config = config

        def list_events(self, mission_id=None, limit=200):
            return ev_rows

        def list_mcp_tool_calls(self, mission_id=None, limit=100):
            return mcp or []

        def get_coding_task_result(self, tid):
            return None

        def session_scope(self):
            @contextlib.contextmanager
            def s():
                if not session_ok:
                    raise RuntimeError("no db")
                yield object()
            return s()

    return RT()


# ── automatic owner_full recording ────────────────────────────────────────────────────────────
def test_owner_recording_gate(env):
    assert rr.owner_recording_enabled(_runtime(mode="owner_full")) is True
    assert rr.owner_recording_enabled(_runtime(mode="off")) is False
    # recording is a no-op (returns None) when collection is off — nothing is written
    assert rr.record_mission_completion(_runtime(mode="off"), "m", "r", {}) is None


def test_completed_mission_writes_a_local_record(env):
    rt = _runtime(events=[("coding_task.started", "t started", {"task_id": "task-9"}),
                          ("mission.completed", "done", {})],
                  mcp=[types.SimpleNamespace(tool_name="build_flowgraph", status="ok",
                                             backend_id="rfmcp", created_at=None)])
    acct = {"coordinator_provider": "catgpt_gateway", "coding_provider": "codex_cli",
            "mcp_used": True, "mcp_tools_called": ["build_flowgraph"]}
    receipt = rr.record_mission_completion(rt, "m-1", "r-1", acct)
    assert receipt is not None and receipt["dest"] == "raw"

    spool = ResearchSpool()
    assert spool.counts()["raw"] == 1
    assert spool.mission_record_count() == 1
    assert spool.last_recorded_at() is not None

    body = spool.read_raw_record(spool.raw_record_digests()[0])
    assert body["schema"] == "aithernet.research.mission.v1"
    assert body["tool_accountability"]["coordinator_provider"] == "catgpt_gateway"
    assert body["coding_task_ids"] == ["task-9"]           # extracted from event payload
    assert body["mcp_tool_calls"][0]["tool_name"] == "build_flowgraph"
    assert any(e["event_type"] == "mission.completed" for e in body["events"])


def test_record_includes_mission_and_run_metadata(env, monkeypatch):
    """_mission_and_run reads real repository fields (mission.content, run.final_response)."""
    import aithernet.state.repositories as repos

    class _MRepo:
        def __init__(self, s): ...
        def get(self, mid):
            return types.SimpleNamespace(source_type="user", status="completed",
                                         created_at=None, content="scan a band")

    class _RRepo:
        def __init__(self, s): ...
        def get(self, rid):
            return types.SimpleNamespace(status="completed", iteration_count=3,
                                         coordinator_call_count=2, coding_agent_action_count=1,
                                         mcp_action_count=1, elapsed_seconds=4.2,
                                         started_at=None, completed_at=None,
                                         final_response="done")

    monkeypatch.setattr(repos, "MissionRepository", _MRepo)
    monkeypatch.setattr(repos, "MissionExecutionRunRepository", _RRepo)
    rt = _runtime(session_ok=True)
    rec = rr.build_mission_record(rt, "m-2", "r-2", {"mcp_used": False})
    assert rec["mission"]["content"] == "scan a band"
    assert rec["run"]["final_response"] == "done"
    assert rec["run"]["iteration_count"] == 3


def test_secrets_are_screened_out_of_records(env):
    """A bearer token in an event message / accountability is redacted — never written to raw."""
    rt = _runtime(events=[("mission.note", "Authorization: Bearer sk-abcdef1234567890TOKEN", {})])
    acct = {"api_key": "sk-should-be-removed", "coordinator_provider": "catgpt_gateway"}
    receipt = rr.record_mission_completion(rt, "m-3", "r-3", acct)
    assert receipt["dest"] == "raw" and receipt["quarantined"] is False
    blob = ResearchSpool().read_raw_record(ResearchSpool().raw_record_digests()[0])
    text = json.dumps(blob)
    assert "sk-should-be-removed" not in text          # secret key value removed
    assert "sk-abcdef1234567890TOKEN" not in text      # bearer token scrubbed
    assert "[redacted]" in text


def test_residual_secret_record_is_quarantined(env):
    spool = ResearchSpool()
    from aithernet.data.redaction import RedactionResult

    class _Leaky:
        def redact(self, payload):
            return RedactionResult(redacted={"leak": "Authorization: Bearer abcdef1234567890xyz"})

    spool._redactor = _Leaky()
    r = spool.write_raw_record({"mission_id": "m"})
    assert r["dest"] == "quarantine" and r["quarantined"] is True
    assert spool.counts()["raw"] == 0 and spool.counts()["quarantine"] == 1


# ── collect / sync ────────────────────────────────────────────────────────────────────────────
def test_collect_packages_pending_records(env):
    spool = ResearchSpool()
    spool.write_raw_record({"schema": "aithernet.research.mission.v1", "mission": {"id": "m1"}})
    out = rs.collect()
    assert out["collected"] == 1 and out["package"].startswith("sha256:")
    assert spool.counts()["snapshots"] == 1 and spool.counts()["manifests"] == 1
    # idempotent: nothing new to collect on a second pass
    assert rs.collect()["collected"] == 0
    assert len(spool.pending_upload()) == 1


def test_sync_refuses_without_oauth_client(env):
    ResearchSpool().write_raw_record({"mission": {"id": "m1"}})
    out = rs.sync()
    assert out["ok"] is False and out["reason"] == "unavailable"
    assert "drive-client install" in (out["next_step"] or "")


def test_sync_refuses_when_not_authorized(env, monkeypatch):
    # OAuth client installed but no refresh token yet.
    monkeypatch.setattr(rs, "drive_state",
                        lambda oauth=None: {"status": "not_authorized",
                                            "next_step": "aithernet data verify --authorize"})
    out = rs.sync()
    assert out["ok"] is False and out["reason"] == "not_authorized"
    assert out["next_step"] == "aithernet data verify --authorize"


def test_sync_mocked_success_updates_upload_ledger(env, monkeypatch):
    spool = ResearchSpool()
    spool.write_raw_record({"mission": {"id": "m1"}})
    rs.collect(spool=spool)
    # Force an authorized Drive and inject a fake archive client.
    monkeypatch.setattr(rs, "drive_state", lambda oauth=None: {"status": "authorized",
                                                               "authorized": True})

    class _FakeClient:
        def find_or_create_folder(self, name, parent):
            return f"folder-{name}"

        def resumable_upload(self, *, folder_id, name, data, idempotency_key, chunk_bytes):
            return {"drive_file_id": "file-" + idempotency_key, "byte_size": len(data)}

        def get_metadata(self, file_id):
            return {"size": None}

    # get_metadata returns no size -> upload_verified falls back to byte_size==len(data): verified.
    out = rs.sync(spool=spool, drive_client=_FakeClient(),
                  collect_first=False, oauth=object())
    assert out["ok"] is True and out["uploaded"] == 1 and out["failed"] == 0
    assert len(spool.uploaded_digests()) == 1
    # idempotent: the same package is skipped on a second sync.
    out2 = rs.sync(spool=spool, drive_client=_FakeClient(), collect_first=False, oauth=object())
    assert out2["uploaded"] == 0 and out2["skipped"] == 1


def test_drive_state_authorized_when_client_present(monkeypatch):
    import aithernet.data.destinations.google_drive_client as gc
    monkeypatch.setattr(gc, "oauth_client_present", lambda: True)
    monkeypatch.setattr(gc, "oauth_client_status", lambda: {"installed": True})
    state = rs.drive_state(oauth=types.SimpleNamespace(authorized=lambda: True))
    assert state["status"] == "authorized" and state["authorized"] is True


# ── CLI surface ───────────────────────────────────────────────────────────────────────────────
def test_required_commands_exist():
    names = {c.name for c in data_app.registered_commands}
    assert {"status", "verify", "collect", "sync", "build", "research-records"} <= names


def test_data_status_after_a_record(env):
    # beta.9: default hosted-owner-archive path — drive is managed by hosted, not the client.
    ResearchSpool().mark_recorded(mission_id="m1", run_id="r1", digest="sha256:x", dest="raw")
    ResearchSpool().write_raw_record({"mission": {"id": "m1"}})
    res = runner.invoke(app, ["data", "status"])
    assert res.exit_code == 0
    out = json.loads(res.stdout)
    assert out["recorder_active"] is True
    assert out["mission_records"] >= 1
    assert out["drive_sync"] == "managed_by_hosted"
    assert out["owner_archive_upload"] == "pending_enrollment"  # not enrolled in the isolated env


def test_data_sync_cli_refuses_and_exits_nonzero(env):
    res = runner.invoke(app, ["data", "sync"])
    assert res.exit_code == 1
    assert "drive-client install" in res.stdout


def test_data_verify_authorize_missing_client_is_actionable(env):
    # beta.9: the client-Drive path is ADVANCED standalone mode only. In that mode, verify
    # --authorize with no OAuth client is still actionable (not a raw DriveError).
    from aithernet.data.research_setup import set_upload_mode
    set_upload_mode("standalone_drive")
    res = runner.invoke(app, ["data", "verify", "--authorize"])
    assert res.exit_code == 1
    assert "drive-client install" in res.stdout
    assert "DriveError" not in res.stdout


def test_data_verify_default_requires_no_client_google(env):
    # beta.9 default hosted path: verify never demands client Google setup.
    res = runner.invoke(app, ["data", "verify"])
    assert res.exit_code == 0
    out = json.loads(res.stdout[:res.stdout.rindex("}") + 1])  # JSON block, then a human line
    assert out["client_google_drive_required"] is False
    assert out["upload_mode"] == "hosted_owner_archive"


def test_data_collect_cli(env):
    ResearchSpool().write_raw_record({"mission": {"id": "m1"}})
    res = runner.invoke(app, ["data", "collect"])
    assert res.exit_code == 0
    assert "Collected 1 record" in res.stdout


def test_drive_client_status_masked(env):
    res = runner.invoke(app, ["data", "drive-client", "status"])
    assert res.exit_code == 0
    out = json.loads(res.stdout)
    assert out["installed"] is False and "drive-client install" in out["repair"]


def test_cli_help_has_no_future_version_labels(env):
    combined = runner.invoke(app, ["data", "--help"]).stdout
    for cmd in ("status", "setup", "verify", "collect", "sync", "research-records", "build"):
        combined += runner.invoke(app, ["data", cmd, "--help"]).stdout
    assert "beta.10" not in combined
    assert "beta.9" not in combined


# ── setup honesty (beta.9 hosted owner-archive default) ─────────────────────────────────────────
def test_setup_default_enables_owner_full_hosted_upload(env):
    from aithernet.data.research_setup import enable_owner_research
    report = enable_owner_research()  # default hosted path; not enrolled in the isolated env
    assert report["collection_mode"] == "owner_full"
    assert report["recorder"] == "active"
    assert report["mode"] == "hosted_owner_archive"
    assert report["consent"] == "granted"
    assert report["owner_archive_upload"] == "pending_enrollment"
    assert ResearchSpool().status()["healthy"] is True


def test_setup_default_requires_no_client_google(env):
    from aithernet.data.research_setup import enable_owner_research
    report = enable_owner_research()  # no oauth client installed — and none is needed
    assert "drive" not in report                    # no client Drive involvement at all
    assert report["mode"] == "hosted_owner_archive"
    # never claims a verified client Drive sync
    assert not any("verified test upload" in s for s in report["steps"])


def test_setup_standalone_drive_without_client_is_unavailable(env):
    from aithernet.data.research_setup import enable_owner_research
    report = enable_owner_research(standalone_drive=True)  # advanced mode, no oauth client
    assert report["mode"] == "standalone_drive"
    assert report["drive"] == "unavailable"
    assert report["drive_reason"] == "oauth-client.json missing"
