"""A deterministic scripted coordinator for real-process integration tests (Stage 13D.3).

Stands in for the non-deterministic Gemini coordinator so two real node PROCESSES can drive a
distributed mission to completion without any model, network, or RF dependency. It uses ONLY the
production coordinator contract + decision router, so the real mission engine performs real,
committed lifecycle transitions (which the production mission-status publisher reports to the
peer). Two roles, selected by ``NODE_ROLE``:

* ``requester`` — a local (user) mission asks the configured peer one question, waits for a
  correlated semantic reply, then completes.
* ``responder`` — an inbound peer-request mission queues exactly one correlated reply (fulfilling
  its response obligation) and then completes, transitioning through real non-terminal states.
"""

from __future__ import annotations

import re

from aithernet.coordinator.contracts import CoordinatorDecision, CoordinatorInput
from aithernet.coordinator.providers.base import CoordinatorProvider

_REQUEST_ID = re.compile(r"request_id=([0-9a-fA-F-]{36})")
_PEER_ID = re.compile(r"peer_id='([0-9a-fA-F-]{36})'")


class ScriptedPeerCoordinator(CoordinatorProvider):
    """Deterministic requester/responder coordinator (no Gemini/Codex/RF/network)."""

    name = "scripted_peer"

    def __init__(self, config, role: str) -> None:
        super().__init__(config)
        self.role = role

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        comm = (payload.mission_run_context or {}).get("communications") or {}
        outbound = comm.get("outbound_messages") or []
        waits = comm.get("reply_waits") or []

        if payload.mission_source_type == "peer_request":
            return self._responder(payload, comm)

        # Requester: ask the peer, wait for the reply, then complete.
        if not outbound:
            peers = (payload.peers or {}).get("peers") or []
            if not peers:
                return self._complete(payload, "No peers available; completed locally.")
            return self._peer_message(payload, {
                "peer_id": peers[0]["peer_id"], "message_type": "request",
                "text": "What are your high-level RF capabilities?", "expects_reply": True,
            })
        if any(w.get("state") == "satisfied" for w in waits):
            return self._complete(
                payload, "Peer replied with capabilities; compared and summarized.")
        msg = outbound[0]
        return self._wait(payload, msg["message_id"])

    def _responder(self, payload: CoordinatorInput, comm: dict) -> CoordinatorDecision:
        # A "[HOLD]" request parks the mission in a STABLE non-terminal `waiting` state (it sends
        # one update to the peer, then waits on a reply that never comes) so the test can prove
        # freshness -> stale -> query -> fresh against a real, persisted non-terminal snapshot.
        if "[HOLD]" in (payload.mission_content or ""):
            outbound = comm.get("outbound_messages") or []
            waits = comm.get("reply_waits") or []
            peer = _PEER_ID.search(payload.mission_content)
            if not outbound and peer:
                # Send a real request back to the peer (expects a reply that never comes), so the
                # next iteration can durably WAIT on it — parking the mission in `waiting`.
                return self._peer_message(payload, {
                    "peer_id": peer.group(1), "message_type": "request",
                    "text": "status hold probe", "expects_reply": True,
                })
            if outbound and not any(w.get("state") == "satisfied" for w in waits):
                return self._wait(payload, outbound[0]["message_id"])
            return self._complete(payload, "hold released")

        obligation = comm.get("response_obligation") or {}
        if obligation.get("response_required") and not obligation.get("response_queued"):
            req = _REQUEST_ID.search(payload.mission_content)
            peer = _PEER_ID.search(payload.mission_content)
            if req and peer:
                return self._peer_message(payload, {
                    "peer_id": peer.group(1), "message_type": "reply",
                    "text": "Capabilities: agent_transport, coordinator_reasoning.",
                    "reply_to_request_id": req.group(1),
                })
        return self._complete(payload, "Handled the peer request and replied.")

    # -- decision builders -------------------------------------------------------

    def _peer_message(self, payload, structured: dict) -> CoordinatorDecision:
        return CoordinatorDecision.from_model_json({
            "summary": "peer message", "next_target": "peer_message", "action": "msg",
            "message": "m", "structured_payload": structured, "expected_result": "ok",
            "mission_control": {"disposition": "continue"},
        }, mission_id=payload.mission_id)

    def _wait(self, payload, message_id: str) -> CoordinatorDecision:
        return CoordinatorDecision.from_model_json({
            "summary": "await reply", "next_target": "respond", "action": "wait", "message": "m",
            "structured_payload": {}, "expected_result": "reply",
            "mission_control": {
                "disposition": "wait",
                "waiting": {"reason": "awaiting peer reply",
                            "wait_condition": {"type": "peer_reply",
                                               "outbound_message_id": message_id}},
            },
        }, mission_id=payload.mission_id)

    def _complete(self, payload, text: str) -> CoordinatorDecision:
        return CoordinatorDecision.from_model_json({
            "summary": "done", "next_target": "respond", "action": "finish", "message": text,
            "structured_payload": {}, "expected_result": "done",
            "mission_control": {"disposition": "complete", "final_response": text},
        }, mission_id=payload.mission_id)
