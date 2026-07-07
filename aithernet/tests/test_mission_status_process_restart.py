"""Stage 13D.3 REAL process remote mission-status + restart + durable-outbox proof.

Unlike the in-process smoke, this launches Node A and Node B as SEPARATE operating-system
processes (the production `_node_proc.py` entry point) driven by a deterministic scripted
coordinator, so a real distributed mission runs through real, committed lifecycle transitions
that the production mission-status publisher reports over the signed durable outbox. It then:

  * terminates the ENTIRE Node A OS process and relaunches it against the same persisted
    db/identity/store/config, proving the remote snapshot, sequence, state, conversation,
    status-event history and topology survive a real restart with no duplicate snapshot;
  * while Node A is down, lets Node B commit a NEW transition whose signed status cannot deliver,
    confirms the unsent outbox record by reading Node B's persistent database directly, terminates
    and restarts the Node B process, and proves the durable outbox delivers the record afterward
    WITHOUT allocating a new sequence or a duplicate publication.

Every wait polls persisted state with strict timeouts; all subprocesses are killed in `finally`;
no orphan is left; logs are preserved on failure.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from aithernet.comms.status_protocol import MissionStatusMessage, MissionStatusType
from aithernet.transport.envelope import MessageKind, build_envelope
from aithernet.transport.identity import IdentityManager

_LAUNCHER = str(Path(__file__).with_name("_node_proc.py"))


def _free_port() -> int:
    """Pick an ephemeral free port so back-to-back runs never collide on a TIME_WAIT port."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Ports + URLs are assigned per test run (see the test body) — these are placeholders the
# helpers close over via the module-level names, set at the top of the test.
_A_PORT = _B_PORT = 0
_A_URL = _B_URL = ""


def _env(node_id, port, base: Path, role: str, *, freshness="300"):
    return {
        **os.environ,
        "NODE_ID": node_id, "PORT": str(port),
        "DB_PATH": str(base / "n.db"),
        "IDENTITY_DIR": str(base / "identity"),
        "STORE_DIR": str(base / "artifact-store"),
        "NODE_ROLE": role, "MISSION_EXEC": "1", "STATUS_FRESHNESS": freshness,
    }


def _launch(env, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "ab")
    return subprocess.Popen([sys.executable, _LAUNCHER], env=env, stdout=log, stderr=log)


def _wait_health(url, *, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return False


def _post(url, path, body=None):
    return httpx.post(f"{url}{path}", json=body, timeout=20)


def _get(url, path):
    return httpx.get(f"{url}{path}", timeout=20)


def _patch(url, path, body):
    return httpx.patch(f"{url}{path}", json=body, timeout=20)


def _await(fn, predicate, *, timeout, interval=0.2):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if predicate(last):
                return last
        except (httpx.HTTPError, sqlite3.Error):
            pass
        time.sleep(interval)
    return last


def _wait_port_free(port, *, timeout=20.0):
    """After SIGKILL the kernel may hold a port briefly; wait until it can be re-bound so a
    same-port restart never races on TIME_WAIT (deterministic, not an arbitrary sleep)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return True
        except OSError:
            s.close()
            time.sleep(0.1)
    return False


def _alive(proc):
    return proc is not None and proc.poll() is None


def _kill(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _sql(db_path: Path, query: str, params=()):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _b_identity(b_dir: Path):
    return IdentityManager(b_dir / "identity", node_id="node-B", node_name="node-B").load()


def _inject(identity, origin_node_id, *, remote_ref, request_id, state, sequence, conv=None):
    """Build a SIGNED status envelope addressed to Node A and POST it to A's authenticated
    inbound endpoint — a real, authenticated transport delivery (not an in-process call)."""
    msg = MissionStatusMessage(
        message_type=MissionStatusType.UPDATE, origin_node_id=origin_node_id,
        remote_mission_id=remote_ref, inbound_request_id=request_id, state=state,
        sequence=sequence, conversation_id=conv)
    env = build_envelope(
        identity=identity, recipient_node_id="node-A", recipient_agent_id=None,
        kind=MessageKind.AGENT_MESSAGE.value, payload=msg.to_payload(),
        conversation_id=conv, correlation_id=request_id, content_type="application/json")
    return _post(_A_URL, "/agent/v1/messages", env.authenticated_dict())


def _setup_peer(url, name, peer_node, peer_url, peer_pub):
    pid = _post(url, "/peers", {"name": name, "role": "node", "endpoint_url": peer_url,
                "expected_node_id": peer_node, "public_key": peer_pub}).json()["id"]
    _post(url, f"/peers/{pid}/trust", {"public_key": peer_pub})
    return pid


def test_real_process_remote_status_restart_and_durable_outbox():
    global _A_PORT, _B_PORT, _A_URL, _B_URL
    _A_PORT, _B_PORT = _free_port(), _free_port()
    _A_URL = f"http://127.0.0.1:{_A_PORT}"
    _B_URL = f"http://127.0.0.1:{_B_PORT}"
    root = Path(tempfile.mkdtemp(prefix="13d3-proc-"))
    a_dir, b_dir = root / "node-A", root / "node-B"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    a_log, b_log = root / "node-A.log", root / "node-B.log"
    procs: list[subprocess.Popen] = []
    failures: list[str] = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    try:
        # ===== A. Start two REAL processes =====================================
        a = _launch(_env("node-A", _A_PORT, a_dir, "requester", freshness="3"), a_log)
        b = _launch(_env("node-B", _B_PORT, b_dir, "responder"), b_log)
        procs += [a, b]
        a_pid, b_pid = a.pid, b.pid
        assert _wait_health(_A_URL, timeout=45) and _wait_health(_B_URL, timeout=45), \
            "nodes did not become healthy"
        check("A. both processes alive + readiness", _alive(a) and _alive(b)
              and _get(_A_URL, "/agent/v1/health").json()["status"] == "ok"
              and _get(_B_URL, "/agent/v1/health").json()["status"] == "ok")

        # mutual trust + all required permissions (separate from trust).
        _post(_A_URL, "/identity/initialize")
        _post(_B_URL, "/identity/initialize")
        a_pub = _get(_A_URL, "/identity/public").json()["public_key"]
        b_pub = _get(_B_URL, "/identity/public").json()["public_key"]
        pb = _setup_peer(_A_URL, "B", "node-B", _B_URL, b_pub)   # B as seen by A
        pa = _setup_peer(_B_URL, "A", "node-A", _A_URL, a_pub)   # A as seen by B
        # B authorizes A's coordinator requests + may send/answer status; A accepts B's status.
        _patch(_B_URL, f"/peers/{pa}/permissions",
               {"may_send_requests": True, "may_request_response": True})
        _patch(_B_URL, f"/peers/{pa}/mission-status-permissions",
               {"may_receive_mission_status": True, "may_query_mission_status": True})
        _patch(_A_URL, f"/peers/{pb}/mission-status-permissions",
               {"may_publish_mission_status": True})
        identity_b = _b_identity(b_dir)

        # ===== B. Real distributed mission ====================================
        mid_a = _post(_A_URL, "/missions",
                      {"content": "Ask the peer for capabilities.",
                       "source_type": "user"}).json()["id"]
        _post(_A_URL, f"/missions/{mid_a}/start")
        snaps = _await(lambda: _get(_A_URL, "/remote-missions").json(),
                       lambda s: len(s) >= 1 and s[0].get("latest_sequence", 0) > 0, timeout=40)
        check("B. Node A persisted a remote snapshot with sequence > 0",
              snaps and snaps[0]["latest_sequence"] > 0)
        snap = snaps[0]
        sid = snap["snapshot_id"]
        check("B10. correct origin/peer/request/conversation correlation",
              snap["origin_node_id"] == "node-B" and snap["peer_id"] == pb
              and snap["request_id"] and snap["conversation_id"]
              and snap["local_mission_id"] == mid_a)
        remote_ref = snap["remote_mission_ref"]
        req_id, conv = snap["request_id"], snap["conversation_id"]

        # ===== C. Real lifecycle transitions -> monotonic sequences ===========
        term = _await(lambda: _get(_A_URL, f"/remote-missions/{sid}").json(),
                      lambda s: s.get("terminal_category") == "completed", timeout=40)
        check("16-17. A displays authenticated terminal state",
              term.get("state") == "completed" and term["terminal_category"] == "completed"
              and term["freshness"] == "terminal")
        events = _get(_A_URL, f"/remote-missions/{sid}/events").json()
        accepted = [e["sequence"] for e in events if e["disposition"] == "accepted"]
        check("12. strictly increasing accepted sequences (>=2 transitions)",
              len(accepted) >= 2 and accepted == sorted(accepted)
              and len(set(accepted)) == len(accepted))
        terminal_seq = term["latest_sequence"]

        # 13. duplicate (same seq + same state) changes nothing.
        _inject(identity_b, "node-B", remote_ref=remote_ref, request_id=req_id,
                state="completed", sequence=terminal_seq, conv=conv)
        time.sleep(0.5)
        check("13. duplicate update changes nothing",
              _get(_A_URL, f"/remote-missions/{sid}").json()["latest_sequence"] == terminal_seq)
        # 14. older sequence cannot replace newer state.
        _inject(identity_b, "node-B", remote_ref=remote_ref, request_id=req_id,
                state="active", sequence=1, conv=conv)
        time.sleep(0.5)
        s_after = _get(_A_URL, f"/remote-missions/{sid}").json()
        check("14. older update cannot replace newer state",
              s_after["latest_sequence"] == terminal_seq and s_after["state"] == "completed")
        # 15. same sequence + conflicting payload is rejected.
        _inject(identity_b, "node-B", remote_ref=remote_ref, request_id=req_id,
                state="failed", sequence=terminal_seq, conv=conv)
        time.sleep(0.5)
        evs = _get(_A_URL, f"/remote-missions/{sid}/events").json()
        check("15. same-sequence conflicting payload rejected",
              any(e["disposition"] == "invalid_transition_rejected" for e in evs)
              and _get(_A_URL, f"/remote-missions/{sid}").json()["state"] == "completed")

        # 18. status is not an ACK, not a semantic reply, did not satisfy a wait, made no
        # MissionStep. A's mission completed via the SEMANTIC REPLY; its reply wait is satisfied.
        comm = _get(_A_URL, f"/missions/{mid_a}/communications").json()
        check("18a. A's reply wait satisfied by the semantic reply (separate from status)",
              any(w["state"] == "satisfied" for w in comm.get("reply_waits", [])))
        steps_before = len(_get(_A_URL, f"/missions/{mid_a}/steps").json())
        # A duplicate status delivery (same sequence) — still processed, still no MissionStep,
        # and it does not advance the snapshot (so the later real query/response correlates).
        _inject(identity_b, "node-B", remote_ref=remote_ref, request_id=req_id,
                state="completed", sequence=terminal_seq, conv=conv)
        time.sleep(0.5)
        check("18b. inbound status created no MissionStep on A",
              len(_get(_A_URL, f"/missions/{mid_a}/steps").json()) == steps_before)

        # ===== F. Freshness lifecycle (real signed non-terminal updates) ======
        # A real signed non-terminal update creates a fresh snapshot; with the 3s window it goes
        # stale (state UNCHANGED — never inferred as failed/completed); a newer accepted update
        # makes it fresh again. All are authenticated transport deliveries over real HTTP.
        _inject(identity_b, "node-B", remote_ref="rm-fresh-test", request_id=req_id,
                state="active", sequence=5, conv=conv)
        fresh1 = _await(lambda: [s for s in _get(_A_URL, "/remote-missions").json()
                                 if s["remote_mission_ref"] == "rm-fresh-test"],
                        lambda lst: bool(lst), timeout=15)
        check("35. real non-terminal remote snapshot created (fresh)",
              fresh1 and fresh1[0]["state"] == "active" and fresh1[0]["freshness"] == "fresh")
        hsid = fresh1[0]["snapshot_id"] if fresh1 else None
        if hsid:
            stale = _await(lambda: _get(_A_URL, f"/remote-missions/{hsid}").json(),
                           lambda s: s["freshness"] == "stale", timeout=20)
            check("36-37. stale non-terminal snapshot stays 'active' (not failed/completed)",
                  stale["freshness"] == "stale" and stale["state"] == "active")
            _inject(identity_b, "node-B", remote_ref="rm-fresh-test", request_id=req_id,
                    state="active", sequence=6, conv=conv)
            refreshed = _await(lambda: _get(_A_URL, f"/remote-missions/{hsid}").json(),
                               lambda s: s["freshness"] == "fresh", timeout=15)
            check("40. a newer accepted update makes the snapshot fresh again",
                  refreshed["freshness"] == "fresh" and refreshed["latest_sequence"] == 6)

        # 38-39. A real SIGNED status query A->B; B answers from persisted mission facts; A
        # accepts the response and clears the pending-query flag.
        _post(_A_URL, f"/remote-missions/{sid}/query")
        answered = _await(lambda: _get(_A_URL, f"/remote-missions/{sid}").json(),
                          lambda s: s["query_pending"] is False, timeout=20)
        check("38-39. real query answered from persisted fact (pending cleared, state intact)",
              answered["query_pending"] is False and answered["state"] == "completed")

        # ===== E-setup. Queue a Node B transition that cannot deliver (A down) =
        # Send a second request; once B owns the inbound mission, stop A so B's status updates
        # for it cannot be delivered and pile up, unsent, in B's durable outbox.
        r2 = _post(_A_URL, "/communications/send",
                   {"peer_id": pb, "message_type": "request",
                    "text": "second request", "expects_reply": False}).json()
        req2 = r2["request_id"]
        _await(lambda: _sql(b_dir / "n.db",
                            "SELECT mission_id FROM inbound_requests WHERE request_id=?", (req2,)),
               lambda rows: rows and rows[0][0], timeout=30)
        mid_b2 = _sql(b_dir / "n.db",
                      "SELECT mission_id FROM inbound_requests WHERE request_id=?", (req2,))[0][0]

        # ===== D + E. Terminate A; B's status for mission2 cannot deliver =====
        pre = _get(_A_URL, f"/remote-missions/{sid}").json()
        topo_before = _get(_A_URL, f"/missions/{mid_a}/distributed-timeline").json()["topology"]
        node_count_before = len(topo_before["nodes"])
        _kill(a)
        check("20-21. Node A process terminated", not _alive(a))

        # Read B's PERSISTENT database directly until an UNSENT status outbox record exists for
        # mission2 (mission transition committed + publication exists + outbox record unsent).
        def _b_unsent():
            pubs = _sql(b_dir / "n.db",
                        "SELECT last_sequence FROM local_mission_status_publications "
                        "WHERE mission_id=?", (mid_b2,))
            unsent = _sql(b_dir / "n.db",
                          "SELECT message_id,status FROM agent_outbox_messages "
                          "WHERE application_type='mission_status' "
                          "AND status IN ('pending','failed') "
                          "AND correlation_id=?", (req2,))
            return {"pubs": pubs, "unsent": unsent}
        bstate = _await(_b_unsent, lambda s: s["pubs"] and s["pubs"][0][0] >= 1 and s["unsent"],
                        timeout=40)
        check("25-26. B committed a transition + a publication + an UNSENT outbox record",
              bstate["pubs"] and bstate["pubs"][0][0] >= 1 and bstate["unsent"])
        outbox_msg_id_before = bstate["unsent"][0][0] if bstate["unsent"] else None
        b2_seq_before = bstate["pubs"][0][0] if bstate["pubs"] else None

        # 28-31. Terminate the entire Node B process and restart it (new PID, same state).
        _kill(b)
        check("28-29. Node B process terminated", not _alive(b))
        _wait_port_free(_B_PORT)
        b2 = _launch(_env("node-B", _B_PORT, b_dir, "responder"), b_log)
        procs[1] = b2
        assert _wait_health(_B_URL, timeout=40), "Node B did not restart"
        check("31. Node B restarted with a new PID", b2.pid != b_pid)

        # 22-23. Restart Node A (new PID) against the same persisted state.
        _wait_port_free(_A_PORT)
        a2 = _launch(_env("node-A", _A_PORT, a_dir, "requester", freshness="3"), a_log)
        procs[0] = a2
        assert _wait_health(_A_URL, timeout=40), "Node A did not restart"
        check("23. Node A restarted with a new PID", a2.pid != a_pid)

        # 24. After A restart: mission1 snapshot + sequence + state + conversation + events +
        # topology persist; no duplicate snapshot.
        post = _get(_A_URL, f"/remote-missions/{sid}").json()
        check("24. A restart preserved snapshot id/sequence/state/conversation",
              post["snapshot_id"] == sid and post["latest_sequence"] == pre["latest_sequence"]
              and post["state"] == pre["state"]
              and post["conversation_id"] == pre["conversation_id"])
        check("24b. status-event history persists across restart",
              len(_get(_A_URL, f"/remote-missions/{sid}/events").json()) >= len(events))
        all_for_ref = [s for s in _get(_A_URL, "/remote-missions").json()
                       if s["remote_mission_ref"] == remote_ref and s["peer_id"] == pb]
        check("24c. no duplicate snapshot created by restart", len(all_for_ref) == 1)
        topo_after = _get(_A_URL, f"/missions/{mid_a}/distributed-timeline").json()["topology"]
        check("44. topology persists across restart (>= prior node count)",
              len(topo_after["nodes"]) >= node_count_before
              and any(n["kind"] == "remote_mission" for n in topo_after["nodes"]))

        # 33-34. The durable outbox delivers B's pending mission2 status after both restarts,
        # WITHOUT allocating a new sequence (the persisted record carries its sequence).
        s2 = _await(lambda: [s for s in _get(_A_URL, "/remote-missions").json()
                             if s["request_id"] == req2],
                    lambda lst: bool(lst), timeout=40)
        check("33. durable outbox delivered the unsent status after restart", bool(s2))
        b2_seq_after = _sql(b_dir / "n.db",
                            "SELECT last_sequence FROM local_mission_status_publications "
                            "WHERE mission_id=?", (mid_b2,))
        check("34a. no new sequence allocated on retry (publication sequence unchanged)",
              b2_seq_after and b2_seq_after[0][0] == b2_seq_before)
        outbox_after = _sql(b_dir / "n.db",
                            "SELECT message_id FROM agent_outbox_messages "
                            "WHERE application_type='mission_status' "
                            "AND correlation_id=? AND message_id=?",
                            (req2, outbox_msg_id_before))
        check("13/34. same persisted outbox message id before and after restart (no duplicate)",
              bool(outbox_after))
        pubs2 = _sql(b_dir / "n.db",
                     "SELECT COUNT(*) FROM local_mission_status_publications WHERE mission_id=?",
                     (mid_b2,))
        check("34b. exactly one logical publication for mission2 (no duplicate)",
              pubs2 and pubs2[0][0] == 1)
        if s2:
            s2sid = s2[0]["snapshot_id"]
            # Wait for mission2 to settle (it transitions to terminal), then prove an older retry
            # cannot replace the latest accepted sequence (re-read the live sequence first).
            settled = _await(lambda: _get(_A_URL, f"/remote-missions/{s2sid}").json(),
                             lambda s: s.get("terminal_category") is not None, timeout=30)
            live_seq = settled["latest_sequence"]
            _inject(identity_b, "node-B", remote_ref=settled["remote_mission_ref"],
                    request_id=req2, state="received", sequence=1,
                    conv=settled["conversation_id"])
            time.sleep(0.5)
            check("34c. older retry cannot replace the accepted newer sequence",
                  _get(_A_URL, f"/remote-missions/{s2sid}").json()["latest_sequence"] == live_seq)

        # ===== H. Authorization + wrong-origin rejection ======================
        # 46. wrong-origin: B is the authenticated sender but claims another origin -> rejected.
        before_seq = _get(_A_URL, f"/remote-missions/{sid}").json()["latest_sequence"]
        _inject(identity_b, "node-IMPOSTER", remote_ref=remote_ref, request_id=req_id,
                state="failed", sequence=terminal_seq + 50, conv=conv)
        time.sleep(0.5)
        check("46. wrong-origin status cannot update the snapshot",
              _get(_A_URL, f"/remote-missions/{sid}").json()["latest_sequence"] == before_seq)
        # 45. an unauthenticated/untrusted third peer cannot publish (rejected at transport).
        third = IdentityManager(root / "node-C-id", node_id="node-C",
                                node_name="node-C").initialize()
        cmsg = MissionStatusMessage(message_type=MissionStatusType.UPDATE, origin_node_id="node-C",
                                    remote_mission_id=remote_ref, inbound_request_id=req_id,
                                    state="failed", sequence=999)
        cenv = build_envelope(identity=third, recipient_node_id="node-A", recipient_agent_id=None,
                              kind=MessageKind.AGENT_MESSAGE.value, payload=cmsg.to_payload(),
                              content_type="application/json")
        cresp = _post(_A_URL, "/agent/v1/messages", cenv.authenticated_dict())
        check("45. untrusted third peer rejected (no snapshot change)",
              cresp.status_code >= 400
              and _get(_A_URL, f"/remote-missions/{sid}").json()["latest_sequence"] == before_seq)

        # ===== H. Sanitization scan (APIs + events + subprocess logs) =========
        blobs = " ".join([
            _get(_A_URL, "/remote-missions").text,
            _get(_A_URL, f"/remote-missions/{sid}").text,
            _get(_A_URL, f"/remote-missions/{sid}/events").text,
            _get(_A_URL, f"/missions/{mid_a}/distributed-timeline").text,
            _get(_A_URL, f"/peers/{pb}/mission-status-permissions").text,
            _get(_A_URL, "/events").text,
            a_log.read_text(errors="ignore") if a_log.exists() else "",
            b_log.read_text(errors="ignore") if b_log.exists() else "",
        ]).lower()
        for marker in ("-----begin", "private_key", '"signature"', "envelope_json",
                       "coordinator prompt", "tool_result", str(root).lower(),
                       str(a_dir / "n.db").lower()):
            check(f"47. no sensitive marker '{marker}' exposed", marker not in blobs)

        assert not failures, "FAILED checks: " + "; ".join(failures)
    finally:
        # ===== I. Process cleanup — no orphans ================================
        for p in procs:
            _kill(p)
        if os.environ.get("KEEP_STATUS_LOGS"):
            for log in (a_log, b_log):
                if log.exists():
                    print(f"--- {log} ---\n{log.read_text()[-3000:]}")
    # 50. No process matching the launcher remains (allow a moment for SIGKILL reaping).
    for _ in range(20):
        out = subprocess.run(["pgrep", "-af", "_node_proc.py"], capture_output=True, text=True)
        if "_node_proc.py" not in out.stdout:
            break
        time.sleep(0.1)
    assert "_node_proc.py" not in out.stdout, f"orphan node processes: {out.stdout}"
