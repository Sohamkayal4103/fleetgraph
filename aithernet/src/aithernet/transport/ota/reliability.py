"""Phase 2: the reliability layer — stop-and-wait ARQ over an abstract frame channel.

Sends a message as bounded DATA frames, waits for a per-frame ACK, retransmits up to a bounded
count, and suppresses duplicates on the receiver. Bounded by ``max_retries`` — never an unbounded
retransmission loop. The byte-channel (modem + RF channel) is injected, so this is testable without
any radio.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aithernet.transport.ota.frame import Frame, FrameType, fragment, reassemble
from aithernet.transport.ota.profiles import RFLinkProfile

#: A frame channel: hand it encoded frame bytes, get back the peer's response bytes (or None on a
#: lost frame). The modem + RF channel live behind this in the real transport.
FrameChannel = Callable[[bytes], "bytes | None"]


@dataclass
class SendStats:
    delivered: bool
    frames_sent: int = 0
    frames_acked: int = 0
    retries: int = 0


@dataclass
class ReassemblyState:
    total: int
    by_seq: dict = field(default_factory=dict)

    def add(self, frame: Frame) -> bool:
        """Return True if this fragment is newly stored (False = duplicate suppressed)."""
        if frame.seq in self.by_seq:
            return False
        self.by_seq[frame.seq] = frame
        return True

    def complete(self) -> bool:
        return len(self.by_seq) == self.total


def send_message(payload: bytes, *, msg_id: int, profile: RFLinkProfile,
                 channel: FrameChannel) -> SendStats:
    """Send a message reliably via stop-and-wait ARQ. ``channel`` returns the ACK frame bytes."""
    frames = fragment(payload, msg_id=msg_id, max_frame_payload=profile.max_frame_payload_bytes)
    stats = SendStats(delivered=False)
    for f in frames:
        encoded = f.encode(profile.crc_scheme)
        acked = False
        for attempt in range(profile.max_retries + 1):
            stats.frames_sent += 1
            if attempt:
                stats.retries += 1
            resp = channel(encoded)
            if resp is None:
                continue
            try:
                ack = Frame.decode(resp, profile.crc_scheme)
            except Exception:  # noqa: BLE001 — a corrupt ACK just triggers a bounded retry
                continue
            if ack.frame_type == FrameType.ACK and ack.msg_id == f.msg_id and ack.seq == f.seq:
                acked = True
                stats.frames_acked += 1
                break
        if not acked:
            return stats          # bounded: give up after max_retries on this frame
    stats.delivered = True
    return stats


class Receiver:
    """Accepts inbound DATA frames, suppresses duplicates, ACKs, and reassembles messages."""

    def __init__(self, profile: RFLinkProfile) -> None:
        self.profile = profile
        self._messages: dict[int, ReassemblyState] = {}
        self._completed: dict[int, bytes] = {}

    def on_frame(self, raw: bytes) -> tuple[bytes | None, bytes | None]:
        """Process an inbound encoded frame. Returns (ack_bytes, completed_message_or_None)."""
        frame = Frame.decode(raw, self.profile.crc_scheme)   # raises on CRC failure
        if frame.frame_type != FrameType.DATA:
            return None, None
        state = self._messages.setdefault(frame.msg_id, ReassemblyState(total=frame.total))
        state.add(frame)            # duplicate fragments are suppressed (idempotent)
        ack = Frame(FrameType.ACK, frame.msg_id, frame.seq, frame.total, b"").encode(
            self.profile.crc_scheme)
        if frame.msg_id in self._completed:
            return ack, None        # already delivered this message; ACK again, suppress duplicate
        if state.complete():
            message = reassemble(list(state.by_seq.values()))
            self._completed[frame.msg_id] = message
            return ack, message
        return ack, None
