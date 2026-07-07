"""Phase 3: the simulated RF peer transport — the full canonical peer exchange through RF.

Couples two in-process endpoints through the reference modem + an impaired synthetic channel and
carries canonical signed :class:`MessageEnvelope` objects end to end:

    serialize -> AEAD seal -> fragment -> frame(CRC) -> modulate -> impaired channel ->
    demodulate -> deframe -> reassemble -> AEAD open (replay/expiry/receiver) -> envelope

No mission logic lives here; the receiving node's ingress/policy remain authoritative. No radio is
touched — this is the ``simulated_rf`` transport used for software qualification.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from aithernet.transport.envelope import MessageEnvelope
from aithernet.transport.ota import channel as ch
from aithernet.transport.ota import modem, security
from aithernet.transport.ota.interface import (
    DeliveryReceipt,
    DeliveryStatus,
    TransportCapabilities,
    TransportHealth,
)
from aithernet.transport.ota.profiles import RFLinkProfile
from aithernet.transport.ota.reliability import Receiver, send_message


def derive_link_key(node_a: str, node_b: str, shared_secret: bytes) -> bytes:
    """Deterministic 32-byte AEAD link key for a peer PAIR (order-independent)."""
    import hashlib
    import hmac
    lo, hi = sorted((node_a, node_b))
    msg = f"aithernet-ota-link:{lo}:{hi}".encode()
    return hmac.new(shared_secret, msg, hashlib.sha256).digest()


@dataclass
class RFExchangeRecord:
    """Secret-free record of one message's RF transit (for research collection, Phase 4)."""

    message_id: str
    sender_node_id: str
    receiver_node_id: str
    profile_id: str
    modulation: str
    canonical_payload_digest: str
    serialized_size: int
    encrypted_size: int
    frame_count: int
    retries: int
    acknowledgements: int
    delivery_latency_ms: float
    channel: dict
    decode_outcome: str
    received_payload_digest: str | None
    signature_result: str
    replay_result: str
    simulated: bool = True

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


@dataclass
class _Endpoint:
    node_id: str
    peer_id: str
    peer_public_key_b64: str
    link_key: bytes
    sequence: int = 0
    replay_guard: security.ReplayGuard = field(default_factory=security.ReplayGuard)


class SimulatedRFLink:
    """Owns two paired endpoints + the modem/channel and runs reliable canonical exchanges."""

    transport_id = "simulated_rf"

    def __init__(self, profile: RFLinkProfile,
                 channel_model: ch.ChannelModel | None = None) -> None:
        self.profile = profile
        self.channel = channel_model or ch.ChannelModel(snr_db=15.0)
        self._endpoints: dict[str, _Endpoint] = {}
        self._failures = 0

    def add_endpoint(self, node_id: str, *, peer_id: str, peer_public_key_b64: str,
                     shared_secret: bytes) -> None:
        key = derive_link_key(node_id, peer_id, shared_secret)
        self._endpoints[node_id] = _Endpoint(node_id=node_id, peer_id=peer_id,
                                              peer_public_key_b64=peer_public_key_b64, link_key=key)

    async def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            transport_id=self.transport_id, kind="simulated_rf", supports_ack=True,
            supports_fragmentation=True, max_message_bytes=self.profile.max_message_bytes,
            profiles=(self.profile.profile_id,), tx_requires_authorization=False, physical=False)

    async def health(self) -> TransportHealth:
        return TransportHealth(transport_id=self.transport_id, healthy=self._failures < 3,
                               consecutive_failures=self._failures)

    def transmit(self, *, sender: str, receiver: str, envelope: MessageEnvelope
                 ) -> tuple[DeliveryReceipt, MessageEnvelope | None, RFExchangeRecord]:
        """Run the FULL canonical exchange sender->receiver through RF. Returns
        (receipt, received_envelope_or_None, exchange_record). No radio is touched."""
        s_ep, r_ep = self._endpoints[sender], self._endpoints[receiver]
        t0 = time.monotonic()
        s_ep.sequence += 1
        header, ciphertext = security.seal(
            envelope, key=s_ep.link_key, sender_node_id=sender, receiver_node_id=receiver,
            peer_key_id=s_ep.peer_id, sequence=s_ep.sequence,
            transport_profile_id=self.profile.profile_id)
        rx = Receiver(self.profile)
        # The OTA payload that gets fragmented/modulated = len(header-json) || header-json || ct.
        ota_payload = self._pack(header, ciphertext)
        received_msg: list[bytes] = []

        def frame_channel(encoded: bytes):
            # modulate -> impaired channel -> demodulate -> receiver on_frame -> ACK back
            samples = modem.modulate(encoded, self.profile)
            impaired = ch.apply_channel(samples, self.channel)
            recovered = modem.demodulate(impaired, self.profile).data
            try:
                ack, msg = rx.on_frame(recovered)
            except Exception:  # noqa: BLE001 — a corrupted frame fails CRC -> sender retries
                return None
            if msg is not None:
                received_msg.append(msg)
            return ack

        import hashlib
        msg_id = int.from_bytes(hashlib.sha256(envelope.message_id.encode()).digest()[:8], "big")
        stats = send_message(ota_payload, msg_id=msg_id, profile=self.profile,
                             channel=frame_channel)
        latency_ms = (time.monotonic() - t0) * 1000.0
        record = self._record(envelope, sender, receiver, ciphertext, stats, latency_ms)

        if not stats.delivered or not received_msg:
            self._failures += 1
            record.decode_outcome = "undelivered"
            return (DeliveryReceipt(self.transport_id, envelope.message_id, DeliveryStatus.FAILED,
                                    correlation_id=envelope.correlation_id,
                                    profile_id=self.profile.profile_id,
                                    frames_sent=stats.frames_sent, frames_acked=stats.frames_acked,
                                    retries=stats.retries, latency_ms=latency_ms), None, record)
        self._failures = 0
        # Receiver-side: unpack + AEAD open + replay/expiry/receiver validation.
        try:
            rheader, rct = self._unpack(received_msg[0])
            opened = security.open_sealed(rheader, rct, key=r_ep.link_key,
                                          expected_receiver=receiver,
                                          replay_guard=r_ep.replay_guard)
            sig_ok = (opened.signature is None
                      or opened.verify_signature_with(r_ep.peer_public_key_b64))
            record.decode_outcome = "decoded"
            record.received_payload_digest = rheader.payload_digest
            record.signature_result = "verified" if sig_ok else "unverified"
            record.replay_result = "accepted"
        except security.OTASecurityError as exc:
            record.decode_outcome = f"rejected:{exc}"
            record.replay_result = "rejected"
            return (DeliveryReceipt(self.transport_id, envelope.message_id, DeliveryStatus.REJECTED,
                                    correlation_id=envelope.correlation_id,
                                    profile_id=self.profile.profile_id, detail=str(exc),
                                    retries=stats.retries, latency_ms=latency_ms), None, record)
        return (DeliveryReceipt(self.transport_id, envelope.message_id, DeliveryStatus.DELIVERED,
                                correlation_id=envelope.correlation_id,
                                profile_id=self.profile.profile_id, frames_sent=stats.frames_sent,
                                frames_acked=stats.frames_acked, retries=stats.retries,
                                latency_ms=latency_ms), opened, record)

    # -- pack/unpack the OTA security header + ciphertext into framed bytes -------------------
    @staticmethod
    def _pack(header: security.OTASecurityHeader, ciphertext: bytes) -> bytes:
        import json
        from dataclasses import asdict
        hjson = json.dumps(asdict(header), sort_keys=True, separators=(",", ":")).encode()
        return len(hjson).to_bytes(2, "big") + hjson + ciphertext

    @staticmethod
    def _unpack(raw: bytes) -> tuple[security.OTASecurityHeader, bytes]:
        import json
        hlen = int.from_bytes(raw[:2], "big")
        header = security.OTASecurityHeader(**json.loads(raw[2:2 + hlen]))
        return header, raw[2 + hlen:]

    def _record(self, env, sender, receiver, ciphertext, stats, latency_ms) -> RFExchangeRecord:
        import hashlib
        import json
        # Same canonical serialization as security.seal so the digests are comparable.
        ser = json.dumps(env.model_dump(mode="json"), sort_keys=True,
                         separators=(",", ":")).encode()
        return RFExchangeRecord(
            message_id=env.message_id, sender_node_id=sender, receiver_node_id=receiver,
            profile_id=self.profile.profile_id, modulation=self.profile.modulation,
            canonical_payload_digest="sha256:" + hashlib.sha256(ser).hexdigest(),
            serialized_size=len(ser), encrypted_size=len(ciphertext), frame_count=stats.frames_sent,
            retries=stats.retries, acknowledgements=stats.frames_acked,
            delivery_latency_ms=latency_ms, channel=self.channel.to_dict(),
            decode_outcome="pending", received_payload_digest=None,
            signature_result="unknown", replay_result="unknown")
