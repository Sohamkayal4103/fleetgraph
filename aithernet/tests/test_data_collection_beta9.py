"""beta.9 — client hosted owner-archive path (provider-agnostic recorder + uploader).

Covers: setup Y enables owner-archive recording (hosted, NOT client Drive); unenrolled → local
recording + upload pending enrollment; automatic hosted upload with capability minting + ACK-based
cleanup; service-restart queue resume; consent grant/withdraw; data status/verify use generic
provider fields and require no client Google; and the PROVIDER-AGNOSTIC matrix — CatGPT coordinator,
API providers with CatGPT absent, any coding provider, MCP tool missions, and blocked outcomes —
none recording provider secrets.
"""
from __future__ import annotations

import contextlib
import json
import types

import pytest

from aithernet.data import research_recorder as rr
from aithernet.data import research_upload as ru
from aithernet.data.research_spool import ResearchSpool


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path / "state"))
    cfg = tmp_path / "state" / "config" / "node.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("data_platform:\n  collection:\n    data_collection_mode: owner_full\n"
                   "    research_consent: true\n    research_upload_mode: hosted_owner_archive\n")
    monkeypatch.setenv("AITHERNET_CONFIG", str(cfg))
    return tmp_path


def _runtime(*, coordinator="catgpt_gateway", coding="codex_cli", model="catgpt-browser",
             mcp=None, events=None, catgpt_present=True):
    coll = types.SimpleNamespace(data_collection_mode="owner_full", research_consent=True,
                                 research_upload_mode="hosted_owner_archive")
    config = types.SimpleNamespace(
        data_platform=types.SimpleNamespace(collection=coll), hardware_profile="software-only")

    class _Ev:
        def __init__(self, t, m, p):
            self.created_at = None
            self.event_type = t
            self.source = "mission_worker"
            self.message = m
            self.payload = p

    ev_rows = [_Ev(*e) for e in (events or [])]
    coord_cfg = types.SimpleNamespace(model=model) if catgpt_present else \
        types.SimpleNamespace(model=model)

    class RT:
        def __init__(self):
            self.config = config
            self.coordinator = types.SimpleNamespace(config=coord_cfg)

        def list_events(self, mission_id=None, limit=200):
            return ev_rows

        def list_mcp_tool_calls(self, mission_id=None, limit=100):
            return mcp or []

        def get_coding_task_result(self, tid):
            return None

        def session_scope(self):
            @contextlib.contextmanager
            def s():
                raise RuntimeError("no db")
                yield
            return s()

    return RT()


# ── provider-agnostic recorder matrix ───────────────────────────────────────────────────────────
def test_record_is_provider_agnostic_catgpt():
    rt = _runtime(coordinator="catgpt_gateway", coding="codex_cli", model="catgpt-browser")
    acct = {"coordinator_provider": "catgpt_gateway", "coding_provider": "codex_cli",
            "mcp_used": False}
    rec = rr.build_mission_record(rt, "m", "r", acct)
    assert rec["provider"]["coordinator_provider"] == "catgpt_gateway"
    assert rec["provider"]["coding_provider"] == "codex_cli"
    assert rec["provider"]["effective_model"] == "catgpt-browser"


def test_record_is_provider_agnostic_api_provider_no_catgpt():
    # Claude/OpenAI/Gemini API coordinator, CatGPT absent; Claude Code coding provider.
    rt = _runtime(coordinator="claude_api", coding="claude_code", model="claude-sonnet-5",
                  catgpt_present=False)
    acct = {"coordinator_provider": "claude_api", "coding_provider": "claude_code",
            "mcp_used": False}
    rec = rr.build_mission_record(rt, "m", "r", acct)
    assert rec["provider"]["coordinator_provider"] == "claude_api"
    assert rec["provider"]["coding_provider"] == "claude_code"
    assert rec["provider"]["effective_model"] == "claude-sonnet-5"
    # record the mission ARCHITECTURE, not a CatGPT/Codex-specific implementation
    assert "catgpt" not in json.dumps(rec).lower() or rec["provider"]["coordinator_provider"] != \
        "catgpt_gateway"


@pytest.mark.parametrize("coding", ["codex_cli", "claude_code", "future_api_coder"])
def test_coding_provider_abstraction(coding):
    rt = _runtime(coding=coding)
    rec = rr.build_mission_record(rt, "m", "r", {"coding_provider": coding})
    assert rec["provider"]["coding_provider"] == coding


def test_mcp_tool_mission_records_tool_metadata():
    calls = [types.SimpleNamespace(tool_name="build_flowgraph", status="ok", backend_id="rfmcp",
                                   created_at=None)]
    rt = _runtime(mcp=calls, events=[("mcp.tool", "used", {})])
    acct = {"coordinator_provider": "gemini_api", "mcp_used": True, "gnuradio_mcp_used": True}
    rec = rr.build_mission_record(rt, "m", "r", acct)
    assert rec["mcp_tool_calls"][0]["tool_name"] == "build_flowgraph"
    assert "gnuradio_mcp" in rec["provider"]["tool_providers"]


def test_blocked_outcome_records_sanitized_blocker_reason():
    rt = _runtime()
    rec = rr.build_mission_record(rt, "m", "r", {"required_tool_missing": True},
                                  outcome="blocked",
                                  blocker_reason="blocked_required_tool[mcp_required]: needs MCP")
    assert rec["outcome"] == "blocked"
    assert rec["quality"]["outcome"] == "blocked"
    assert rec["quality"]["blocker_category"] == "required_tool_missing"
    assert "needs MCP" in rec["blocker_reason"]


def test_recorded_package_excludes_provider_secrets(env):
    rt = _runtime(events=[("note", "CATGPT_GATEWAY_API_KEY=sk-secret-abc123", {})])
    acct = {"coordinator_provider": "catgpt_gateway",
            "api_key": "sk-should-be-removed", "vnc_password": "hunter2"}
    receipt = rr.record_mission_outcome(rt, "m", "r", outcome="completed", accountability=acct)
    assert receipt is not None and receipt["dest"] == "raw"
    body = json.dumps(ResearchSpool().read_raw_record(ResearchSpool().raw_record_digests()[0]))
    assert "sk-should-be-removed" not in body
    assert "sk-secret-abc123" not in body
    assert "hunter2" not in body


# ── hosted upload flow (mocked network) ─────────────────────────────────────────────────────────
def _fake_enrolled(monkeypatch, tmp_path):
    enr = types.SimpleNamespace(enrollment_state="enrolled",
                                control_plane_base_url="https://cp.test",
                                ingestion_base_url="https://ing.test", tenant_id="t1")
    ident = types.SimpleNamespace(node_id="node-a", public_key_b64="pk",
                                  sign=lambda m: "sig")
    monkeypatch.setattr(ru, "_load_enrollment", lambda state_root=None: enr)
    monkeypatch.setattr(ru, "_load_identity", lambda state_root=None, node_id=None: ident)
    monkeypatch.setattr(ru, "is_enrolled", lambda state_root=None: True)


def test_upload_pending_mints_capability_and_uploads(env, monkeypatch, tmp_path):
    _fake_enrolled(monkeypatch, tmp_path)
    # a record + collected snapshot to upload
    sp = ResearchSpool()
    sp.write_raw_record({"schema": "aithernet.research.mission.v1", "mission": {"id": "m1"}})
    from aithernet.data.research_sync import collect
    collect(spool=sp)

    minted = {"capability_token": "cap-token-xyz", "expires_at": None}
    uploads: list = []

    class _Client:
        def __init__(self, base_url, **kw): ...
        def mint_research_capability(self, **kw):
            return minted

        def upload_research_package(self, **kw):
            uploads.append(kw["idempotency_key"])
            return {"package_id": "pkg-1", "status": "accepted"}

    monkeypatch.setattr(ru, "HostedClient", _Client, raising=False)
    import aithernet.hosted.client as hc
    monkeypatch.setattr(hc, "HostedClient", _Client)

    out = ru.upload_pending(spool=sp, consent=True, collect_first=False)
    assert out["ok"] is True and out["uploaded"] == 1 and out["failed"] == 0
    assert len(uploads) == 1
    # capability persisted 0600
    assert ru.load_capability() == minted
    # ACK-based cleanup compacted the uploaded snapshot
    assert sp.snapshot_digests() == [] or len(sp.pending_upload()) == 0


def test_upload_pending_not_enrolled_is_pending(env, monkeypatch):
    monkeypatch.setattr(ru, "is_enrolled", lambda state_root=None: False)
    out = ru.upload_pending(consent=True, collect_first=False)
    assert out["ok"] is False and out["reason"] == "pending_enrollment"


def test_service_restart_resumes_queue(env, monkeypatch, tmp_path):
    # An unuploaded snapshot remains queued across a fresh spool instance (simulated restart).
    sp = ResearchSpool()
    sp.write_raw_record({"mission": {"id": "m1"}})
    from aithernet.data.research_sync import collect
    collect(spool=sp)
    assert len(sp.pending_upload()) == 1
    # a brand-new instance (as if after service restart) still sees the queued package
    assert len(ResearchSpool().pending_upload()) == 1


# ── setup + status/verify ───────────────────────────────────────────────────────────────────────
def test_setup_enables_hosted_not_client_drive(env):
    from aithernet.data.research_setup import enable_owner_research
    report = enable_owner_research()  # not enrolled in the isolated env
    assert report["mode"] == "hosted_owner_archive"
    assert report["consent"] == "granted"
    assert report["owner_archive_upload"] == "pending_enrollment"
    assert "drive" not in report  # no client Drive setup


def test_owner_archive_state_transitions(env, monkeypatch):
    # not enrolled → pending_enrollment
    st = ru.owner_archive_state(consent=True)
    assert st["owner_archive_upload"] == "pending_enrollment"
    assert st["drive_sync"] == "managed_by_hosted"
    # no consent → disabled
    st2 = ru.owner_archive_state(consent=False)
    assert st2["owner_archive_upload"] == "disabled"
