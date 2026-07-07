"""Stage 13D.2 real two-node artifact BYTE-plane tests (Part L/M).

Starts two real Aithernet nodes (separate ids/identities/dbs/ports/state roots/artifact stores)
as in-thread uvicorn servers and drives the authenticated streaming download over real HTTP:
full transfer + verify + import, resume from a persisted partial offset, expired-grant refusal,
unauthorized refusal, and confirmation that no bytes/paths/keys leak. Uses no Gemini/Codex/RF.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from aithernet.api.app import create_app
from aithernet.config.settings import (
    AgentTransportConfig,
    ArtifactConfig,
    IdentityConfig,
    MissionExecutionConfig,
    NodeConfig,
    TransportLocalDevelopmentConfig,
    TransportOutboundConfig,
)
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.state.models import utcnow
from aithernet.state.repositories import ArtifactTransferRepository


def _runtime(root: Path, node_id, port):
    base = root / node_id
    base.mkdir(parents=True, exist_ok=True)
    cfg = NodeConfig(
        node_id=node_id, node_name=node_id, host="127.0.0.1", port=port,
        database_url=f"sqlite:///{base / 'n.db'}", log_level="warning",
        mission_execution=MissionExecutionConfig(enabled=False),
        agent_transport=AgentTransportConfig(
            identity=IdentityConfig(state_directory=str(base / "identity")),
            outbound=TransportOutboundConfig(enabled=True, poll_interval_seconds=0.2),
            local_development=TransportLocalDevelopmentConfig(allow_insecure_http=True),
        ),
        artifacts=ArtifactConfig(
            enabled=True, state_directory=str(base / "artifact-store"), chunk_bytes=64 * 1024,
            worker_poll_interval_seconds=0.2, minimum_free_bytes=0,
        ),
    )
    return NodeRuntime.from_config(cfg)


class _Server:
    def __init__(self, runtime, port):
        self.runtime, self.url = runtime, f"http://127.0.0.1:{port}"
        self._s = uvicorn.Server(uvicorn.Config(create_app(runtime=runtime), host="127.0.0.1",
                                                port=port, log_level="warning"))
        self._t = threading.Thread(target=self._s.run, daemon=True)

    def start(self):
        self._t.start()
        deadline = time.monotonic() + 10
        while not self._s.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server start timeout")
            time.sleep(0.02)

    def stop(self):
        self._s.should_exit = True
        self._t.join(timeout=5)


def _post(url, path, body=None):
    with httpx.Client(base_url=url, timeout=20) as c:
        return c.post(path, json=body)


def _get(url, path):
    with httpx.Client(base_url=url, timeout=20) as c:
        return c.get(path)


def _patch(url, path, body):
    with httpx.Client(base_url=url, timeout=20) as c:
        return c.patch(path, json=body)


def _await_state(url, tid, *, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = _get(url, f"/artifact-transfers/{tid}").json()
        if t["state"] in ("completed", "failed", "rejected", "cancelled", "expired"):
            return t
        time.sleep(0.2)
    return _get(url, f"/artifact-transfers/{tid}").json()


@pytest.fixture(scope="module")
def two_servers():
    root = Path(tempfile.mkdtemp(prefix="13d2-test-"))
    a = _Server(_runtime(root, "node-A", 8690), 8690)
    b = _Server(_runtime(root, "node-B", 8691), 8691)
    a.start()
    b.start()
    _post(a.url, "/identity/initialize")
    _post(b.url, "/identity/initialize")
    a_pub = _get(a.url, "/identity/public").json()["public_key"]
    b_pub = _get(b.url, "/identity/public").json()["public_key"]
    pb = _post(a.url, "/peers", {"name": "B", "role": "node", "endpoint_url": b.url,
               "expected_node_id": "node-B", "public_key": b_pub}).json()["id"]
    _post(a.url, f"/peers/{pb}/trust", {"public_key": b_pub})
    pa = _post(b.url, "/peers", {"name": "A", "role": "node", "endpoint_url": a.url,
               "expected_node_id": "node-A", "public_key": a_pub}).json()["id"]
    _post(b.url, f"/peers/{pa}/trust", {"public_key": a_pub})
    try:
        yield {"a": a, "b": b, "pb": pb, "pa": pa, "root": root}
    finally:
        a.stop()
        b.stop()


def _index_on_b(servers, data, name="cap.bin"):
    src = servers["root"] / "node-B" / name
    src.write_bytes(data)
    return servers["b"].runtime.artifacts.index_local_artifact(
        source_path=str(src), backend_id="marconi", artifact_kind="capture", display_name=name,
    )


def test_full_transfer_verifies_and_imports(two_servers):
    s = two_servers
    blob = os.urandom(300 * 1024)
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    art = _index_on_b(s, blob)
    _patch(s["b"].url, f"/peers/{s['pa']}/artifact-permissions",
           {"may_request_artifacts": True, "may_receive_artifacts": True})
    req = _post(s["a"].url, "/artifact-transfers/request",
                {"peer_id": s["pb"], "origin_artifact_id": art["artifact_id"],
                 "digest": digest, "size": len(blob)}).json()
    final = _await_state(s["a"].url, req["transfer_id"])
    assert final["state"] == "completed"
    # Verified import inside A's managed store.
    imported = [a for a in _get(s["a"].url, "/artifacts").json()
                if a["digest"] == digest and a["availability_state"] == "imported"]
    assert len(imported) == 1 and imported[0]["size_bytes"] == len(blob)
    obj = (s["root"] / "node-A" / "artifact-store" / "objects" / "sha256"
           / digest.split(":")[1][:2] / f"sha256_{digest.split(':')[1]}")
    assert obj.is_file()
    assert hashlib.sha256(obj.read_bytes()).hexdigest() == digest.split(":")[1]
    # No partial left exposed; no bytes/paths/keys in API responses.
    assert not (s["root"] / "node-A" / "artifact-store" / "partial" / req["transfer_id"]).exists()
    blob_txt = (_get(s["a"].url, "/artifacts").text + _get(s["a"].url, "/artifact-transfers").text
                + _get(s["a"].url, "/artifact-store/status").text).lower()
    assert "signature" not in blob_txt and str(s["root"]).lower() not in blob_txt


def test_resume_from_persisted_partial(two_servers):
    s = two_servers
    blob = os.urandom(256 * 1024)
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    art = _index_on_b(s, blob, name="resume.bin")
    _patch(s["b"].url, f"/peers/{s['pa']}/artifact-permissions",
           {"may_request_artifacts": True, "may_receive_artifacts": True})
    req = _post(s["a"].url, "/artifact-transfers/request",
                {"peer_id": s["pb"], "origin_artifact_id": art["artifact_id"],
                 "digest": digest, "size": len(blob)}).json()
    tid = req["transfer_id"]
    # Wait for the grant, then PRE-SEED a partial (first half) + reset state to simulate an
    # interrupted download; the worker must RESUME from the persisted offset, not restart.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _get(s["a"].url, f"/artifact-transfers/{tid}").json()["state"] in (
            "authorized", "transferring", "completed"
        ):
            break
        time.sleep(0.1)
    arts = s["a"].runtime.artifacts
    half = len(blob) // 2
    arts.store.append_chunk(tid, offset=0, data=blob[:half], max_total_bytes=len(blob))
    with s["a"].runtime.session_scope() as session:
        ArtifactTransferRepository(session).update(tid, fields={
            "state": "authorized", "received_bytes": half, "next_attempt_at": utcnow(),
            "claim_owner": None, "claim_expires_at": None,
        })
        session.commit()
    s["a"].runtime.schedule_artifact_worker()
    final = _await_state(s["a"].url, tid)
    assert final["state"] == "completed"
    obj = (s["root"] / "node-A" / "artifact-store" / "objects" / "sha256"
           / digest.split(":")[1][:2] / f"sha256_{digest.split(':')[1]}")
    assert obj.is_file() and hashlib.sha256(obj.read_bytes()).hexdigest() == digest.split(":")[1]


def test_expired_grant_cannot_download(two_servers):
    s = two_servers
    blob = os.urandom(80 * 1024)
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    art = _index_on_b(s, blob, name="expired.bin")
    _patch(s["b"].url, f"/peers/{s['pa']}/artifact-permissions",
           {"may_request_artifacts": True, "may_receive_artifacts": True})
    req = _post(s["a"].url, "/artifact-transfers/request",
                {"peer_id": s["pb"], "origin_artifact_id": art["artifact_id"],
                 "digest": digest, "size": len(blob)}).json()
    tid = req["transfer_id"]
    # Force-expire B's grant immediately so the pull is refused (410) -> A fails permanently.
    time.sleep(1.0)
    with s["b"].runtime.session_scope() as session:
        from datetime import timedelta
        ArtifactTransferRepository(session).update(
            tid, fields={"grant_expires_at": utcnow() - timedelta(seconds=10)}
        )
        session.commit()
    # Re-arm A's transfer (clear any partial progress) and let the worker attempt the pull.
    with s["a"].runtime.session_scope() as session:
        ArtifactTransferRepository(session).update(
            tid, fields={"state": "authorized", "next_attempt_at": utcnow()}
        )
        session.commit()
    s["a"].runtime.schedule_artifact_worker()
    final = _await_state(s["a"].url, tid, timeout=15)
    assert final["state"] in ("failed", "expired")


def test_unauthorized_request_is_rejected(two_servers):
    s = two_servers
    blob = os.urandom(40 * 1024)
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    art = _index_on_b(s, blob, name="unauth.bin")
    # B does NOT authorize this artifact request.
    _patch(s["b"].url, f"/peers/{s['pa']}/artifact-permissions", {"may_request_artifacts": False})
    req = _post(s["a"].url, "/artifact-transfers/request",
                {"peer_id": s["pb"], "origin_artifact_id": art["artifact_id"],
                 "digest": digest, "size": len(blob)}).json()
    final = _await_state(s["a"].url, req["transfer_id"], timeout=15)
    assert final["state"] in ("rejected", "failed")
    assert not any(a["digest"] == digest and a["availability_state"] == "imported"
                   for a in _get(s["a"].url, "/artifacts").json())
