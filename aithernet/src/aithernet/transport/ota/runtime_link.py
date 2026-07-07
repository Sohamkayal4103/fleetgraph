"""Item 1: the runtime RF carrier bridging the OTA stack to the canonical peer ingress.

Wraps the tested :class:`SimulatedRFLink` so a node's transport service can deliver a canonical
``MessageEnvelope`` to a trusted peer over simulated RF and receive the peer's signed result back —
the full bidirectional path:

    sender -> seal/modulate/channel/demod/open -> RECEIVER INGRESS (canonical, authoritative) ->
    result envelope -> RF return path -> sender receives the semantic result

The receiver ingress is injected (the real node's inbound handler), so no parallel RF-only mission
logic exists and the receiving node keeps full policy ownership. The link key is a per-pair AEAD key
derived from an out-of-band pairing secret (the SDR pre-shared-key model); the canonical envelope's
Ed25519 signature still binds peer identity. Physical TX is never invoked; no key material logged.
"""

from __future__ import annotations

from collections.abc import Callable

from aithernet.transport.envelope import MessageEnvelope
from aithernet.transport.identity import NodeIdentity
from aithernet.transport.ota import channel as ch
from aithernet.transport.ota.interface import DeliveryReceipt, DeliveryStatus
from aithernet.transport.ota.profiles import REFERENCE_BPSK, RFLinkProfile
from aithernet.transport.ota.simulated_rf import RFExchangeRecord, SimulatedRFLink

#: A receiver ingress: given the opened canonical envelope, return a signed result/ACK envelope
#: (or None). This is the real node's inbound handler — the OTA layer never decides policy.
ReceiverIngress = Callable[[MessageEnvelope], "MessageEnvelope | None"]


class RFRuntimeLink:
    """Delivers canonical envelopes over simulated RF and carries the signed result back."""

    transport_id = "simulated_rf"

    def __init__(self, local_identity: NodeIdentity, *, peer_node_id: str,
                 peer_public_key_b64: str, link_secret: bytes,
                 profile: RFLinkProfile = REFERENCE_BPSK,
                 channel: ch.ChannelModel | None = None) -> None:
        self.local = local_identity
        self.peer_node_id = peer_node_id
        self.peer_public_key_b64 = peer_public_key_b64
        self.profile = profile
        self._link = SimulatedRFLink(profile, channel or ch.ChannelModel(snr_db=15.0))
        self._link.add_endpoint(local_identity.node_id, peer_id=peer_node_id,
                                peer_public_key_b64=peer_public_key_b64, shared_secret=link_secret)
        self._link.add_endpoint(peer_node_id, peer_id=local_identity.node_id,
                                peer_public_key_b64=local_identity.public_key_b64,
                                shared_secret=link_secret)

    def health_ok(self) -> bool:
        return self._link._failures < 3

    def deliver(self, envelope: MessageEnvelope, *, receiver_ingress: ReceiverIngress
                ) -> tuple[DeliveryReceipt, MessageEnvelope | None, list[RFExchangeRecord]]:
        """Deliver ``envelope`` to the peer over RF, run the peer's ingress, and carry the signed
        result back over the RF return path. Returns (receipt, result_envelope, [records])."""
        records: list[RFExchangeRecord] = []
        receipt, opened, rec = self._link.transmit(
            sender=self.local.node_id, receiver=self.peer_node_id, envelope=envelope)
        records.append(rec)
        if receipt.status is not DeliveryStatus.DELIVERED or opened is None:
            return receipt, None, records

        # Receiving node is authoritative: ITS ingress decides + (optionally) returns a result.
        result_env = receiver_ingress(opened)
        if result_env is None:
            return receipt, None, records

        # RF return path: peer -> sender (the signed semantic result).
        back_receipt, back_opened, back_rec = self._link.transmit(
            sender=self.peer_node_id, receiver=self.local.node_id, envelope=result_env)
        records.append(back_rec)
        if back_receipt.status is not DeliveryStatus.DELIVERED or back_opened is None:
            return receipt, None, records
        return receipt, back_opened, records
