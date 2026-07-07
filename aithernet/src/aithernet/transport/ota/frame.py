"""Phase 1/2: byte-level framing + CRC for the OTA reliability layer.

A frame is the smallest acknowledged unit: ``HEADER || PAYLOAD || CRC``. The preamble + sync word
(for acquisition) and FEC/modulation are applied by the modem around these bytes. Frames are
bounded; a logical message is fragmented across DATA frames and reassembled on receipt.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

FRAME_VERSION = 1

# HEADER: version(B) type(B) msg_id(Q) seq(H) total(H) payload_len(H)  -> 16 bytes
_HEADER = struct.Struct(">BBQHHH")
HEADER_LEN = _HEADER.size


class FrameType(IntEnum):
    DATA = 0
    ACK = 1
    NAK = 2
    # beta.3 MAC/link-protocol frame types (carried by the MAC codec in ``mac.py``; the legacy
    # DATA/ACK/NAK above remain the stop-and-wait reliability unit used inside a DATA burst).
    BEACON = 3
    RTS = 4
    CTS = 5
    BLOCK_ACK = 6
    NACK = 7
    CANCEL = 8
    FIN = 9
    DELIVERY_RECEIPT = 10


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


_CRC = {"crc16": (crc16_ccitt, 2), "crc32": (crc32, 4)}


class FrameError(ValueError):
    """Raised on a malformed or CRC-invalid frame."""


@dataclass(frozen=True)
class Frame:
    frame_type: FrameType
    msg_id: int                # uint64 logical-message id
    seq: int                   # fragment index (0-based)
    total: int                 # fragment count
    payload: bytes

    def encode(self, crc_scheme: str = "crc16") -> bytes:
        if crc_scheme not in _CRC:
            raise FrameError(f"unknown crc scheme {crc_scheme!r}")
        if len(self.payload) > 0xFFFF:
            raise FrameError("payload too large for a single frame")
        header = _HEADER.pack(FRAME_VERSION, int(self.frame_type), self.msg_id & 0xFFFFFFFFFFFFFFFF,
                              self.seq & 0xFFFF, self.total & 0xFFFF, len(self.payload))
        body = header + self.payload
        crc_fn, _ = _CRC[crc_scheme]
        crc_val = crc_fn(body)
        crc_bytes = crc_val.to_bytes(_CRC[crc_scheme][1], "big")
        return body + crc_bytes

    @classmethod
    def decode(cls, raw: bytes, crc_scheme: str = "crc16") -> Frame:
        """Parse a frame from the START of ``raw`` using the header's length; a trailing tail (from
        modem byte-recovery) is ignored. CRC covers exactly HEADER||PAYLOAD."""
        if crc_scheme not in _CRC:
            raise FrameError(f"unknown crc scheme {crc_scheme!r}")
        crc_fn, crc_len = _CRC[crc_scheme]
        if len(raw) < HEADER_LEN + crc_len:
            raise FrameError("frame too short")
        version, ftype, msg_id, seq, total, plen = _HEADER.unpack(raw[:HEADER_LEN])
        if version != FRAME_VERSION:
            raise FrameError(f"unsupported frame version {version}")
        frame_len = HEADER_LEN + plen + crc_len
        if len(raw) < frame_len:
            raise FrameError("payload length exceeds available bytes")
        body = raw[:HEADER_LEN + plen]
        crc_bytes = raw[HEADER_LEN + plen:frame_len]
        if crc_fn(body) != int.from_bytes(crc_bytes, "big"):
            raise FrameError("crc mismatch (corrupted frame)")
        try:
            ft = FrameType(ftype)
        except ValueError as exc:
            raise FrameError(f"unknown frame type {ftype}") from exc
        return cls(frame_type=ft, msg_id=msg_id, seq=seq, total=total, payload=body[HEADER_LEN:])


def fragment(payload: bytes, *, msg_id: int, max_frame_payload: int) -> list[Frame]:
    """Split a message payload into bounded DATA frames (at least one, even for empty payload)."""
    if max_frame_payload <= 0:
        raise FrameError("max_frame_payload must be positive")
    chunks = [payload[i:i + max_frame_payload]
              for i in range(0, len(payload), max_frame_payload)] or [b""]
    total = len(chunks)
    return [Frame(FrameType.DATA, msg_id, i, total, c) for i, c in enumerate(chunks)]


def reassemble(frames: list[Frame]) -> bytes:
    """Reassemble DATA frames into the original payload. Raises on missing/duplicate fragments."""
    data = [f for f in frames if f.frame_type == FrameType.DATA]
    if not data:
        raise FrameError("no DATA frames to reassemble")
    total = data[0].total
    if any(f.total != total for f in data):
        raise FrameError("inconsistent fragment totals")
    by_seq: dict[int, bytes] = {}
    for f in data:
        if f.seq in by_seq:
            raise FrameError(f"duplicate fragment {f.seq}")
        by_seq[f.seq] = f.payload
    if set(by_seq) != set(range(total)):
        raise FrameError(f"missing fragments: expected {total}, have {sorted(by_seq)}")
    return b"".join(by_seq[i] for i in range(total))
