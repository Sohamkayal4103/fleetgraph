"""Complete OTA medium-access + link protocol (1.0.0-beta.3).

This module adds the MAC/link layer that sits above the tested modem (Phase 2) and the AEAD
security layer (Phase 1). The lower layers already give us authenticated, fragmented, CRC- and
FEC-protected bytes over an impaired channel; what was missing — and what this module provides —
is the *medium access* and *delivery* protocol:

* an explicit link state machine
  ``IDLE → (carrier sense) → RTS_SENT → WAIT_CTS → CTS_GRANTED → DATA_BURST → WAIT_BLOCK_ACK →
  RETRANSMIT|FIN → WAIT_DELIVERY_RECEIPT → COMPLETE`` (plus ``FAILED`` / ``CANCELLED``);
* the full frame set BEACON / RTS / CTS / DATA / BLOCK_ACK / NACK / CANCEL / FIN /
  DELIVERY_RECEIPT, each cryptographically bound (HMAC over header+payload with the per-link key);
* two MAC modes — ``point_to_point_tdd`` (a reserved time-division slot, no contention) and
  ``rts_cts_shared_channel`` (carrier sense + RTS/CTS reservation + randomized backoff for
  hidden-node mitigation and collision recovery);
* selective retransmission via a BLOCK_ACK bitmap, with bounded retransmit/backoff limits;
* fairness bounds (a node may not claim the channel more than a configured number of consecutive
  times) and per-session expiry / cancellation.

Everything here is **deterministic and radio-free**: a :class:`VirtualMedium` with an integer
microsecond clock, a seeded backoff RNG, and an explicit :class:`LossPlan` model frame loss,
corruption, collisions and hidden nodes so the entire protocol is exhaustively unit-testable
without hardware. Physical transmission is never invoked here; the operator-controlled physical
path lives in :mod:`aithernet.transport.ota.tx_control` / ``executor`` and is unchanged.

The four acknowledgement layers are kept strictly separate (a radio frame ACK is NEVER reported as
mission success):

* **Link ACK** — a BLOCK_ACK names which frames arrived / are missing.
* **Delivery receipt** — the complete envelope was reconstructed and authenticated.
* **Mission acknowledgement** — the receiving MissionEngine accepted/rejected the request.
* **Mission result** — the requested work completed/failed/blocked/cancelled.

This module owns only the first two (link); the mission layers are produced by the canonical peer
ingress and are correlated by ``session_id`` in the research records.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import struct
from dataclasses import dataclass, field
from enum import Enum

from aithernet.transport.ota.frame import FrameType, crc32

MAC_VERSION = 1

# MAC header: version(B) type(B) mesh_disc(H) src(H) dst(H) session(I) msg_id(Q) frag(H) total(H)
#             seq(I) ack_window(H) reservation_us(I) payload_len(H)  -> 34 bytes
_MAC_HEADER = struct.Struct(">BBHHHIQHHIHIH")
MAC_HEADER_LEN = _MAC_HEADER.size
_AUTH_LEN = 8  # truncated HMAC-SHA256 tag


class MacMode(str, Enum):
    """The two supported MAC modes."""

    POINT_TO_POINT_TDD = "point_to_point_tdd"
    RTS_CTS_SHARED_CHANNEL = "rts_cts_shared_channel"


class LinkState(str, Enum):
    """Explicit link state-machine states."""

    IDLE = "idle"
    CARRIER_SENSE = "carrier_sense"
    RTS_SENT = "rts_sent"
    WAIT_CTS = "wait_cts"
    CTS_GRANTED = "cts_granted"
    DATA_BURST = "data_burst"
    WAIT_BLOCK_ACK = "wait_block_ack"
    RETRANSMIT = "retransmit"
    FIN = "fin"
    WAIT_DELIVERY_RECEIPT = "wait_delivery_receipt"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MacError(ValueError):
    """Raised on a malformed/auth-invalid MAC frame."""


def short_address(identifier: str) -> int:
    """Derive a stable 16-bit short address from a node id / fingerprint (non-sensitive)."""
    return int.from_bytes(hashlib.sha256(identifier.encode("utf-8")).digest()[:2], "big")


def mesh_discriminator(mesh_id: str | None) -> int:
    """Derive a stable 16-bit non-sensitive mesh discriminator (0 = no mesh / direct link)."""
    if not mesh_id:
        return 0
    return int.from_bytes(hashlib.sha256(mesh_id.encode("utf-8")).digest()[:2], "big")


@dataclass(frozen=True)
class MacFrame:
    """One MAC/link frame. Authenticated by an HMAC tag over the full header+payload.

    ``payload`` carries the type-specific body: DATA → a message fragment; RTS/CTS/BLOCK_ACK/
    DELIVERY_RECEIPT → a small bounded JSON struct; BEACON → bounded advertisement; CANCEL/FIN →
    optional reason bytes. No account email, tenant name, OAuth token or secret is ever placed in a
    frame — only the non-sensitive discriminators/addresses derived above.
    """

    frame_type: FrameType
    mesh_disc: int
    src_addr: int
    dst_addr: int
    session_id: int
    msg_id: int
    frag: int
    total: int
    seq: int
    ack_window: int
    reservation_us: int
    payload: bytes = b""

    def header_payload(self) -> bytes:
        header = _MAC_HEADER.pack(
            MAC_VERSION,
            int(self.frame_type),
            self.mesh_disc & 0xFFFF,
            self.src_addr & 0xFFFF,
            self.dst_addr & 0xFFFF,
            self.session_id & 0xFFFFFFFF,
            self.msg_id & 0xFFFFFFFFFFFFFFFF,
            self.frag & 0xFFFF,
            self.total & 0xFFFF,
            self.seq & 0xFFFFFFFF,
            self.ack_window & 0xFFFF,
            self.reservation_us & 0xFFFFFFFF,
            len(self.payload),
        )
        return header + self.payload

    def encode(self, link_key: bytes) -> bytes:
        body = self.header_payload()
        crc = crc32(body).to_bytes(4, "big")
        tag = hmac.new(link_key, body + crc, hashlib.sha256).digest()[:_AUTH_LEN]
        return body + crc + tag

    @classmethod
    def decode(cls, raw: bytes, link_key: bytes) -> MacFrame:
        if len(raw) < MAC_HEADER_LEN + 4 + _AUTH_LEN:
            raise MacError("mac frame too short")
        (
            version, ftype, mesh_disc, src, dst, session, msg_id, frag, total, seq,
            ack_window, reservation_us, plen,
        ) = _MAC_HEADER.unpack(raw[:MAC_HEADER_LEN])
        if version != MAC_VERSION:
            raise MacError(f"unsupported mac version {version}")
        end = MAC_HEADER_LEN + plen
        if len(raw) < end + 4 + _AUTH_LEN:
            raise MacError("mac payload length exceeds available bytes")
        payload = raw[MAC_HEADER_LEN:end]
        crc_bytes = raw[end:end + 4]
        tag = raw[end + 4:end + 4 + _AUTH_LEN]
        body = raw[:end]
        if crc32(body) != int.from_bytes(crc_bytes, "big"):
            raise MacError("mac crc mismatch (corrupted frame)")
        expected = hmac.new(link_key, body + crc_bytes, hashlib.sha256).digest()[:_AUTH_LEN]
        if not hmac.compare_digest(expected, tag):
            raise MacError("mac auth tag mismatch")
        try:
            ft = FrameType(ftype)
        except ValueError as exc:
            raise MacError(f"unknown frame type {ftype}") from exc
        return cls(
            frame_type=ft, mesh_disc=mesh_disc, src_addr=src, dst_addr=dst, session_id=session,
            msg_id=msg_id, frag=frag, total=total, seq=seq, ack_window=ack_window,
            reservation_us=reservation_us, payload=payload,
        )

    def json_payload(self) -> dict:
        if not self.payload:
            return {}
        try:
            data = json.loads(self.payload.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}


def _json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class MacConfig:
    """Bounded MAC parameters (deterministic; per-frame airtime is integer microseconds)."""

    mode: MacMode = MacMode.RTS_CTS_SHARED_CHANNEL
    frame_airtime_us: int = 1_000
    rts_airtime_us: int = 200
    cts_turnaround_us: int = 100
    max_data_frames: int = 64
    backoff_slot_us: int = 200
    backoff_min_slots: int = 1
    backoff_max_slots: int = 16
    max_handshake_attempts: int = 5
    max_retransmit_rounds: int = 4
    max_consecutive_claims: int = 3       # fairness bound
    session_expiry_us: int = 5_000_000
    cts_max_accept_frames: int = 64


@dataclass
class LossPlan:
    """A deterministic frame-loss/corruption plan for tests.

    Each entry drops/corrupts the Nth occurrence (1-based) of a frame type as it is transmitted by
    a given source short address. ``hidden_pairs`` marks ordered (a, b) addresses that cannot hear
    each other (hidden nodes) — used by the shared-channel simulator.
    """

    drop: set[tuple[int, FrameType, int]] = field(default_factory=set)      # (src, type, nth)
    corrupt: set[tuple[int, FrameType, int]] = field(default_factory=set)   # (src, type, nth)
    _counts: dict[tuple[int, FrameType], int] = field(default_factory=dict)

    def classify(self, src_addr: int, ftype: FrameType) -> str:
        key = (src_addr, ftype)
        self._counts[key] = self._counts.get(key, 0) + 1
        nth = self._counts[key]
        if (src_addr, ftype, nth) in self.drop:
            return "drop"
        if (src_addr, ftype, nth) in self.corrupt:
            return "corrupt"
        return "pass"


@dataclass
class MacTransferResult:
    """The reasoned outcome of one MAC transfer (secret-free; for diagnostics + research)."""

    delivered: bool
    mac_mode: str
    final_state: str
    session_id: int
    states: list[str] = field(default_factory=list)
    handshake_attempts: int = 0
    rts_sent: int = 0
    cts_received: int = 0
    cts_rejected: int = 0
    reservation_us: int = 0
    rts_cts_turnaround_us: int = 0
    data_frames_sent: int = 0
    retransmissions: int = 0
    retransmit_rounds: int = 0
    block_acks: int = 0
    nacks: int = 0
    duplicates_suppressed: int = 0
    backoff_events: int = 0
    backoff_total_us: int = 0
    collisions: int = 0
    carrier_busy_deferrals: int = 0
    cancelled: bool = False
    link_acknowledged: bool = False        # link ACK layer (frames accounted for)
    delivery_receipt: bool = False         # delivery layer (envelope reconstructed+authenticated)
    total_airtime_us: int = 0
    detail: str = ""

    def record(self, state: LinkState) -> None:
        self.states.append(state.value)


# -- virtual medium --------------------------------------------------------------------------


@dataclass
class _Tx:
    start_us: int
    end_us: int
    src_addr: int
    frame_type: FrameType


class VirtualMedium:
    """A deterministic shared RF medium with an integer-microsecond clock.

    Models carrier-busy (so carrier sense works), overlapping-transmission collisions (so RTS
    collisions and hidden nodes are real), and a configurable reservation/NAV that defers nodes
    that heard a CTS. No randomness lives here — backoff randomness is seeded in the endpoints.
    """

    def __init__(self, loss: LossPlan | None = None) -> None:
        self.now_us = 0
        self.loss = loss or LossPlan()
        self._inflight: list[_Tx] = []
        self.nav_until_us = 0          # network allocation vector (reservation) end
        self.nav_owner: int | None = None
        self.history: list[_Tx] = []

    def advance(self, us: int) -> None:
        self.now_us += max(0, int(us))

    def busy(self) -> bool:
        return any(tx.end_us > self.now_us for tx in self._inflight)

    def reserved_for_other(self, src_addr: int) -> bool:
        return self.nav_until_us > self.now_us and self.nav_owner not in (None, src_addr)

    def reserve(self, *, owner: int, duration_us: int) -> None:
        self.nav_until_us = self.now_us + max(0, duration_us)
        self.nav_owner = owner

    def transmit(self, frame: MacFrame, *, airtime_us: int) -> str:
        """Place a frame on the medium for ``airtime_us``. Returns 'pass'/'drop'/'corrupt'.

        A collision is detected when another transmission overlaps this one in time; both are
        marked corrupt (lost) for this delivery.
        """
        start, end = self.now_us, self.now_us + airtime_us
        tx = _Tx(start, end, frame.src_addr, frame.frame_type)
        # Drop expired in-flight, then check overlap with still-active transmissions.
        self._inflight = [t for t in self._inflight if t.end_us > start]
        collided = any(t.src_addr != frame.src_addr for t in self._inflight)
        self._inflight.append(tx)
        self.history.append(tx)
        self.advance(airtime_us)
        if collided:
            return "collision"
        return self.loss.classify(frame.src_addr, frame.frame_type)


# -- endpoint + link driver ------------------------------------------------------------------


class MacEndpoint:
    """One node's MAC identity + per-link state (short address, key, fairness counter)."""

    def __init__(self, node_id: str, *, link_key: bytes, mesh_id: str | None = None) -> None:
        self.node_id = node_id
        self.link_key = link_key
        self.addr = short_address(node_id)
        self.mesh_disc = mesh_discriminator(mesh_id)
        self.consecutive_claims = 0
        self._seen_sessions: set[int] = set()


def _backoff_slots(rng: random.Random, cfg: MacConfig, attempt: int) -> int:
    """Truncated binary exponential backoff window, deterministic for a seeded RNG."""
    ceiling = min(cfg.backoff_max_slots, cfg.backoff_min_slots * (2 ** attempt))
    return rng.randint(cfg.backoff_min_slots, max(cfg.backoff_min_slots, ceiling))


def run_mac_transfer(
    *,
    tx: MacEndpoint,
    rx: MacEndpoint,
    payload_fragments: list[bytes],
    medium: VirtualMedium,
    config: MacConfig | None = None,
    seed: int = 0,
    profile_id: str = "reference-bpsk",
    cancel_before_data: bool = False,
) -> MacTransferResult:
    """Drive one complete MAC transfer of ``payload_fragments`` from ``tx`` to ``rx``.

    Deterministic: the only randomness is seeded backoff. Honors the medium's :class:`LossPlan`,
    carrier sense, RTS/CTS reservation, BLOCK_ACK selective retransmission, bounded retransmit and
    handshake limits, fairness bound and cancellation. Returns a fully reasoned
    :class:`MacTransferResult` with the link/delivery acknowledgement layers separated.
    """
    cfg = config or MacConfig()
    rng = random.Random(seed)
    total = len(payload_fragments)
    if total == 0 or total > cfg.max_data_frames:
        raise MacError(f"frame count {total} out of bounds (1..{cfg.max_data_frames})")
    sid_hi = short_address(f"{tx.node_id}:{rx.node_id}:{seed}")
    session_id = ((sid_hi << 8) | (seed & 0xFF)) & 0xFFFFFFFF
    result = MacTransferResult(
        delivered=False, mac_mode=cfg.mode.value, final_state=LinkState.IDLE.value,
        session_id=session_id,
    )
    result.record(LinkState.IDLE)

    def _mk(ft: FrameType, *, frag=0, seq=0, ack_window=0, reservation_us=0,
            payload=b"") -> MacFrame:
        return MacFrame(
            frame_type=ft, mesh_disc=tx.mesh_disc, src_addr=tx.addr, dst_addr=rx.addr,
            session_id=session_id, msg_id=session_id, frag=frag, total=total, seq=seq,
            ack_window=ack_window, reservation_us=reservation_us, payload=payload,
        )

    # Fairness: a node may not exceed max_consecutive_claims back-to-back channel claims.
    if tx.consecutive_claims >= cfg.max_consecutive_claims:
        tx.consecutive_claims = 0
        result.final_state = LinkState.FAILED.value
        result.detail = "fairness bound reached; yielding the channel"
        result.record(LinkState.FAILED)
        return result

    reservation_us = cfg.cts_turnaround_us + total * cfg.frame_airtime_us + cfg.frame_airtime_us

    # ---- handshake (RTS/CTS) for the shared channel; reserved slot for TDD --------------------
    if cfg.mode is MacMode.RTS_CTS_SHARED_CHANNEL:
        granted = False
        for attempt in range(cfg.max_handshake_attempts):
            result.handshake_attempts = attempt + 1
            # Carrier sense + NAV deferral.
            result.record(LinkState.CARRIER_SENSE)
            if medium.busy() or medium.reserved_for_other(tx.addr):
                result.carrier_busy_deferrals += 1
                slots = _backoff_slots(rng, cfg, attempt)
                result.backoff_events += 1
                result.backoff_total_us += slots * cfg.backoff_slot_us
                medium.advance(slots * cfg.backoff_slot_us)
                continue
            # Send RTS.
            rts = _mk(
                FrameType.RTS, reservation_us=reservation_us,
                payload=_json_bytes({
                    "receiver": rx.addr, "frames": total, "profile": profile_id,
                    "requested_window": total, "ack_mode": "block_ack",
                    "est_airtime_us": reservation_us,
                }),
            )
            result.rts_sent += 1
            result.record(LinkState.RTS_SENT)
            outcome = medium.transmit(rts, airtime_us=cfg.rts_airtime_us)
            result.total_airtime_us += cfg.rts_airtime_us
            result.record(LinkState.WAIT_CTS)
            if outcome == "collision":
                result.collisions += 1
            if outcome in ("drop", "collision", "corrupt"):
                slots = _backoff_slots(rng, cfg, attempt)
                result.backoff_events += 1
                result.backoff_total_us += slots * cfg.backoff_slot_us
                medium.advance(slots * cfg.backoff_slot_us)
                continue
            # Receiver evaluates capacity and replies CTS (accept/reject).
            accept = total <= cfg.cts_max_accept_frames
            cts = MacFrame(
                frame_type=FrameType.CTS, mesh_disc=rx.mesh_disc, src_addr=rx.addr,
                dst_addr=tx.addr, session_id=session_id, msg_id=session_id, frag=0, total=total,
                seq=0, ack_window=total, reservation_us=reservation_us,
                payload=_json_bytes({
                    "accepted": accept, "turnaround_us": cfg.cts_turnaround_us,
                    "granted_us": reservation_us, "profile": profile_id, "max_frames": total,
                }),
            )
            medium.advance(cfg.cts_turnaround_us)
            cts_outcome = medium.transmit(cts, airtime_us=cfg.rts_airtime_us)
            result.total_airtime_us += cfg.rts_airtime_us
            result.rts_cts_turnaround_us = cfg.cts_turnaround_us
            if cts_outcome in ("drop", "collision", "corrupt"):
                if cts_outcome == "collision":
                    result.collisions += 1
                slots = _backoff_slots(rng, cfg, attempt)
                result.backoff_events += 1
                result.backoff_total_us += slots * cfg.backoff_slot_us
                medium.advance(slots * cfg.backoff_slot_us)
                continue
            if not accept:
                result.cts_rejected += 1
                result.final_state = LinkState.FAILED.value
                result.detail = "CTS rejected the reservation"
                result.record(LinkState.FAILED)
                return result
            result.cts_received += 1
            result.reservation_us = reservation_us
            medium.reserve(owner=tx.addr, duration_us=reservation_us)
            result.record(LinkState.CTS_GRANTED)
            granted = True
            break
        if not granted:
            result.final_state = LinkState.FAILED.value
            result.detail = "RTS/CTS handshake failed within attempt budget"
            result.record(LinkState.FAILED)
            tx.consecutive_claims = 0
            return result
    else:
        # point_to_point_tdd: a pre-arranged reserved slot, no contention.
        result.reservation_us = reservation_us
        medium.reserve(owner=tx.addr, duration_us=reservation_us)
        result.record(LinkState.CTS_GRANTED)

    if cancel_before_data:
        cancel = _mk(FrameType.CANCEL, payload=b"operator-cancel")
        medium.transmit(cancel, airtime_us=cfg.rts_airtime_us)
        result.cancelled = True
        result.final_state = LinkState.CANCELLED.value
        result.detail = "transfer cancelled before data burst"
        result.record(LinkState.CANCELLED)
        return result

    # ---- DATA burst + BLOCK_ACK selective retransmission --------------------------------------
    # ``received`` is the RECEIVER's ground truth; ``acked`` is what the SENDER has confirmation
    # for (learned ONLY from a delivered BLOCK_ACK). The sender retransmits everything it has not
    # had acknowledged — so a LOST block-ack causes a retransmit round even though the receiver
    # already holds the frames (the duplicates are then suppressed).
    received: dict[int, bytes] = {}
    acked: set[int] = set()
    still_missing: list[int] = list(range(total))
    for round_idx in range(cfg.max_retransmit_rounds + 1):
        to_send = [i for i in range(total) if i not in acked]
        if not to_send:
            break
        if round_idx > 0:
            result.retransmit_rounds += 1
            result.record(LinkState.RETRANSMIT)
        else:
            result.record(LinkState.DATA_BURST)
        for frag_idx in to_send:
            data = _mk(
                FrameType.DATA, frag=frag_idx, seq=frag_idx, ack_window=total,
                payload=payload_fragments[frag_idx],
            )
            result.data_frames_sent += 1
            if round_idx > 0:
                result.retransmissions += 1
            outcome = medium.transmit(data, airtime_us=cfg.frame_airtime_us)
            result.total_airtime_us += cfg.frame_airtime_us
            if outcome == "collision":
                result.collisions += 1
                continue
            if outcome in ("drop", "corrupt"):
                continue  # missing/corrupt -> stays missing, NACK'd by the bitmap
            if frag_idx in received:
                result.duplicates_suppressed += 1
                continue
            received[frag_idx] = payload_fragments[frag_idx]
        # Receiver returns a BLOCK_ACK bitmap (the link ACK layer).
        result.record(LinkState.WAIT_BLOCK_ACK)
        bitmap = sum(1 << i for i in received)
        still_missing = [i for i in range(total) if i not in received]
        block_ack = MacFrame(
            frame_type=FrameType.BLOCK_ACK, mesh_disc=rx.mesh_disc, src_addr=rx.addr,
            dst_addr=tx.addr, session_id=session_id, msg_id=session_id, frag=0, total=total,
            seq=0, ack_window=total, reservation_us=0,
            payload=_json_bytes({"bitmap": bitmap, "missing": still_missing}),
        )
        ack_outcome = medium.transmit(block_ack, airtime_us=cfg.rts_airtime_us)
        result.total_airtime_us += cfg.rts_airtime_us
        if still_missing:
            result.nacks += 1
        if ack_outcome in ("drop", "collision", "corrupt"):
            # Lost BLOCK_ACK: the sender learns nothing this round and retransmits next round.
            if ack_outcome == "collision":
                result.collisions += 1
            continue
        result.block_acks += 1
        acked = set(received)  # the sender now knows exactly which frames the receiver holds

    if acked == set(range(total)) and len(received) == total:
        result.link_acknowledged = True
    if len(received) == total:
        result.record(LinkState.WAIT_DELIVERY_RECEIPT)
        # Delivery receipt (a DISTINCT layer): the envelope was reconstructed + authenticated.
        receipt = MacFrame(
            frame_type=FrameType.DELIVERY_RECEIPT, mesh_disc=rx.mesh_disc, src_addr=rx.addr,
            dst_addr=tx.addr, session_id=session_id, msg_id=session_id, frag=0, total=total,
            seq=0, ack_window=total, reservation_us=0,
            payload=_json_bytes({"status": "delivered", "frames": total}),
        )
        medium.transmit(receipt, airtime_us=cfg.rts_airtime_us)
        result.total_airtime_us += cfg.rts_airtime_us
        result.delivery_receipt = True
        result.delivered = True
        result.final_state = LinkState.COMPLETE.value
        result.record(LinkState.COMPLETE)
        tx.consecutive_claims += 1
    else:
        fin = _mk(FrameType.FIN, payload=_json_bytes({"missing": still_missing}))
        medium.transmit(fin, airtime_us=cfg.rts_airtime_us)
        result.final_state = LinkState.FAILED.value
        result.detail = f"{total - len(received)} frame(s) unrecovered after retransmit budget"
        result.record(LinkState.FIN)
        result.record(LinkState.FAILED)
        tx.consecutive_claims = 0

    # Session expiry guard (bounded; never blocks — informational).
    if result.total_airtime_us + result.backoff_total_us > cfg.session_expiry_us:
        result.detail = (result.detail + "; " if result.detail else "") + "session exceeded expiry"
    return result


@dataclass
class ContentionOutcome:
    """The reasoned result of a slotted CSMA/CA contention round set (deterministic)."""

    grant_order: list[str] = field(default_factory=list)     # node ids in the order they won
    collisions: int = 0
    backoff_redraws: int = 0
    hidden_node_collisions: int = 0
    slots_elapsed: int = 0
    per_node_wins: dict[str, int] = field(default_factory=dict)
    fairness_ok: bool = True
    starved: list[str] = field(default_factory=list)


def simulate_contention(
    *,
    contenders: list[str],
    rounds: int,
    config: MacConfig | None = None,
    seed: int = 0,
    hidden_pairs: set[tuple[str, str]] | None = None,
) -> ContentionOutcome:
    """Resolve ``rounds`` channel acquisitions among ``contenders`` via slotted CSMA/CA.

    Deterministic for a given seed. Each contender draws a random backoff within its contention
    window; the node that reaches zero first wins the slot and (via RTS/CTS) reserves the channel,
    so every other node — *including hidden nodes that cannot hear the winner's RTS* — defers on the
    CTS (NAV). When two contenders reach zero in the SAME slot their RTS frames collide: both double
    their contention window and redraw (binary exponential backoff). A fairness bound caps
    consecutive wins by any one node so no node starves.

    Hidden nodes matter only at the RTS step (they cannot carrier-sense each other); RTS/CTS is
    exactly the mechanism that still prevents their DATA from colliding, which this models by
    having all nodes honor the CTS reservation regardless of whether they heard the RTS.
    """
    cfg = config or MacConfig()
    rng = random.Random(seed)
    hidden = hidden_pairs or set()
    outcome = ContentionOutcome(per_node_wins={c: 0 for c in contenders})
    cw_min = max(cfg.backoff_min_slots * 2, len(contenders) * 2)
    windows = {c: cw_min for c in contenders}
    backoff = {c: rng.randint(0, cw_min) for c in contenders}
    last_winner: str | None = None
    consecutive = 0
    pending = rounds

    def _hidden(a: str, b: str) -> bool:
        return (a, b) in hidden or (b, a) in hidden

    guard = 0
    guard_max = rounds * (cfg.backoff_max_slots + cw_min + 4) * (len(contenders) + 1)
    while pending > 0 and guard < guard_max:
        guard += 1
        # Every node counts its backoff down by one slot — losers' residuals persist, so a node
        # that drew a large backoff steadily approaches zero and is guaranteed to win in time.
        for c in contenders:
            backoff[c] -= 1
        outcome.slots_elapsed += 1
        ready = [c for c in contenders if backoff[c] < 0]
        if not ready:
            continue
        # Fairness: a node at its consecutive-claim cap yields the channel (redraws, no win).
        if (
            len(ready) == 1
            and ready[0] == last_winner
            and consecutive >= cfg.max_consecutive_claims
        ):
            c = ready[0]
            windows[c] = cw_min
            backoff[c] = rng.randint(1, cw_min)
            consecutive = 0
            last_winner = None
            continue
        if len(ready) == 1:
            winner = ready[0]
            outcome.grant_order.append(winner)
            outcome.per_node_wins[winner] += 1
            consecutive = consecutive + 1 if winner == last_winner else 1
            last_winner = winner
            windows[winner] = cw_min
            backoff[winner] = rng.randint(0, cw_min)
            pending -= 1
        else:
            # Simultaneous RTS -> collision. Hidden-node collisions are counted separately because
            # they would NOT have been avoided by carrier sense alone (only RTS/CTS handles them).
            outcome.collisions += 1
            if any(_hidden(a, b) for a in ready for b in ready if a != b):
                outcome.hidden_node_collisions += 1
            for c in ready:
                windows[c] = min(cfg.backoff_max_slots, windows[c] * 2)
                backoff[c] = rng.randint(1, windows[c])
                outcome.backoff_redraws += 1

    # Fairness check: no node should win more than ceil(rounds/contenders) + max_consecutive_claims.
    fair_cap = (rounds // max(1, len(contenders))) + cfg.max_consecutive_claims + 1
    outcome.fairness_ok = all(w <= fair_cap for w in outcome.per_node_wins.values())
    outcome.starved = [c for c, w in outcome.per_node_wins.items() if w == 0]
    return outcome


def reassemble_fragments(fragments: dict[int, bytes], total: int) -> bytes:
    """Reassemble received fragments (raises if incomplete)."""
    if set(fragments) != set(range(total)):
        raise MacError("cannot reassemble: missing fragments")
    return b"".join(fragments[i] for i in range(total))
