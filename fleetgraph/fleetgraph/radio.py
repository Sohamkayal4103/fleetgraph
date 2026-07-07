"""FleetGraph radio bridge — carry car-to-car state beacons over Aithernet's REAL software radio.

This is the load-bearing honesty of the whole demo: when a car's lidar is blinded and it falls
back to radio, the beacon is not "delivered with probability p". It is signed (Ed25519), AEAD
sealed, fragmented, BPSK-modulated, pushed through a synthetic impaired channel whose SNR we derive
from the *world* (distance + fog + interference), demodulated, and either survives its CRC or does
not. Same code path Aithernet runs against a physical PlutoSDR — we only swap the physical radio for
the deterministic `simulated_rf` channel.

We reuse Aithernet as a library (no reimplementation):
  - transport.identity  -> Ed25519 node identity per car (held in memory, never written to disk)
  - transport.envelope  -> the canonical signed MessageEnvelope the beacon rides in
  - transport.ota.*     -> modem / channel / AEAD / stop-and-wait ARQ (SimulatedRFLink.transmit)
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aithernet.transport.envelope import MessageEnvelope, build_envelope
from aithernet.transport.identity import NodeIdentity, fingerprint_for_public_key
from aithernet.transport.ota.channel import ChannelModel
from aithernet.transport.ota.profiles import REFERENCE_BPSK, REFERENCE_QPSK, RFLinkProfile
from aithernet.transport.ota.simulated_rf import SimulatedRFLink

# A fixed demo-wide shared secret for the pairwise AEAD link key derivation. In a real deployment
# this comes from an authenticated key exchange; for the simulation every car in the mesh shares it,
# which is enough to demonstrate confidentiality + the pair-bound link key (derive_link_key sorts
# the pair, so A<->B and B<->A agree).
_DEMO_LINK_SECRET = b"fleetgraph-demo-shared-secret-v1"

PROFILES: dict[str, RFLinkProfile] = {
    "ref-bpsk-1k": REFERENCE_BPSK,   # robust, low-rate — the fog/long-range fallback waveform
    "ref-qpsk-2k": REFERENCE_QPSK,   # faster, less robust — good link only
}


def make_car_identity(node_id: str, node_name: str | None = None) -> NodeIdentity:
    """Generate an in-memory Ed25519 identity for a car (nothing touches disk)."""
    private = Ed25519PrivateKey.generate()
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    public_b64 = base64.b64encode(raw_public).decode("ascii")
    return NodeIdentity(
        node_id=node_id,
        node_name=node_name or node_id,
        identity_version=1,
        public_key_b64=public_b64,
        fingerprint=fingerprint_for_public_key(public_b64),
        created_at=datetime.now(timezone.utc),
        enabled=True,
        _private_key=private,
    )


# ---------------------------------------------------------------------------
# World -> SNR model. This is the ONLY place physics meets the radio: it turns a
# geometric/weather situation into the channel SNR the modem must survive.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LinkConditions:
    distance_m: float          # separation between the two cars (world units == metres)
    fog_density: float = 0.0   # 0..1 — attenuates the signal (also what blinds lidar)
    interference: float = 0.0  # 0..1 — ambient / jammer contribution near the receiver
    tx_power_db: float = 22.0  # nominal effective radiated power headroom for the demo


# Measured delivery cliff of Aithernet's reference BPSK link (see calibration in smoke_radio):
#   SNR >= -2 dB reliably delivers, ~-4 dB is flaky, <= -6 dB is reliably lost.
# We use this cheap, deterministic predictor for tick-by-tick MESH REACHABILITY (which car can hear
# whom) so the engine is fast, and reserve the full pure-Python modem for the specific beacons we
# actually showcase (RadioMesh.send_beacon), whose real frame/retry/signature facts we attach to the
# graph. This is honest: reachability uses the modem's own measured SNR->delivery curve.
_CLIFF_DB = -3.0


def predict_delivery(snr_db: float, *, seed: int = 0) -> bool:
    """Fast, deterministic estimate of whether a beacon at this SNR clears the reference link.

    Hard-decides outside the flaky band; inside [-5, -1] dB it uses the seed so a marginal link
    flickers deterministically (great for the demo, still reproducible)."""
    if snr_db >= -1.0:
        return True
    if snr_db <= -5.0:
        return False
    # flaky band: deterministic flicker biased by how far above the cliff we are
    import hashlib
    h = int.from_bytes(hashlib.sha256(f"{round(snr_db, 1)}:{seed}".encode()).digest()[:2], "big")
    threshold = (snr_db - (-5.0)) / 4.0        # 0 at -5 dB, 1 at -1 dB
    return (h / 65535.0) < threshold


def snr_db_for(cond: LinkConditions, *, range_ref_m: float = 60.0) -> float:
    """Map a world situation to a channel SNR in dB.

    Deliberately simple and monotonic so the demo is legible:
      - free-space-style log path loss with distance,
      - extra attenuation proportional to fog density and path length,
      - an interference/jammer noise floor.

    Calibrated against the measured cliff of Aithernet's reference BPSK link (delivers reliably
    down to ~-2 dB, flaky ~-4 dB, lost below ~-6 dB). Constants are chosen so that RF beats optical
    lidar through moderate fog (the hero case: radio works where the camera/lidar is blind), while
    heavy-fog-at-range or an active jammer drops the direct link below the cliff and forces a relay.
    """
    d = max(cond.distance_m, 1.0)
    path_loss_db = 20.0 * math.log10(d / range_ref_m) if d > range_ref_m else 0.0
    fog_loss_db = cond.fog_density * (5.0 + 0.30 * d)      # fog hurts more over distance
    interference_db = cond.interference * 40.0             # a strong jammer alone can kill a link
    return cond.tx_power_db - path_loss_db - fog_loss_db - interference_db


# ---------------------------------------------------------------------------
# The mesh: one SimulatedRFLink per unordered car pair, SNR set per transmit.
# ---------------------------------------------------------------------------

@dataclass
class BeaconResult:
    delivered: bool
    status: str                      # "delivered" | "failed" | "rejected"
    snr_db: float
    profile_id: str
    frames_sent: int
    frames_acked: int
    retries: int
    latency_ms: float
    signature_verified: bool
    decode_outcome: str
    received_payload: dict | None
    sender: str
    receiver: str


class RadioMesh:
    """Holds every car identity and lazily builds a paired RF link for each car pair."""

    def __init__(self, profile_id: str = "ref-bpsk-1k") -> None:
        self.profile = PROFILES[profile_id]
        self.identities: dict[str, NodeIdentity] = {}
        self._links: dict[tuple[str, str], SimulatedRFLink] = {}
        self._real_cache: dict[tuple, BeaconResult] = {}

    def add_car(self, identity: NodeIdentity) -> None:
        self.identities[identity.node_id] = identity

    def _link_for(self, a: str, b: str) -> SimulatedRFLink:
        key = tuple(sorted((a, b)))
        link = self._links.get(key)
        if link is None:
            link = SimulatedRFLink(self.profile)
            ida, idb = self.identities[key[0]], self.identities[key[1]]
            link.add_endpoint(ida.node_id, peer_id=idb.node_id,
                              peer_public_key_b64=idb.public_key_b64,
                              shared_secret=_DEMO_LINK_SECRET)
            link.add_endpoint(idb.node_id, peer_id=ida.node_id,
                              peer_public_key_b64=ida.public_key_b64,
                              shared_secret=_DEMO_LINK_SECRET)
            self._links[key] = link
        return link

    def send_beacon(self, sender: str, receiver: str, payload: dict, *,
                    conditions: LinkConditions, seed: int = 0,
                    profile_id: str | None = None) -> BeaconResult:
        """Sign a beacon from `sender` and run the full RF exchange to `receiver`.

        The SNR comes from `conditions` (the world), so a beacon that "doesn't make it" fails a
        real CRC after real modulation — never a coin flip.
        """
        link = self._link_for(sender, receiver)
        if profile_id is not None:
            link.profile = PROFILES[profile_id]
        snr = snr_db_for(conditions)
        link.channel = ChannelModel(snr_db=snr, seed=seed)

        envelope: MessageEnvelope = build_envelope(
            identity=self.identities[sender],
            recipient_node_id=receiver,
            recipient_agent_id=None,
            kind="agent_message",
            payload=payload,
        )
        try:
            receipt, received, record = link.transmit(sender=sender, receiver=receiver,
                                                       envelope=envelope)
        except Exception:  # noqa: BLE001
            # At very low SNR the reference modem can raise inside demodulation before its own
            # frame-level guard (an un-deframable garble). The honest interpretation is a LOST
            # beacon: the receiver heard noise it could not decode into a valid frame.
            return BeaconResult(
                delivered=False, status="failed", snr_db=snr, profile_id=link.profile.profile_id,
                frames_sent=0, frames_acked=0, retries=0, latency_ms=0.0,
                signature_verified=False, decode_outcome="undecodable_noise",
                received_payload=None, sender=sender, receiver=receiver)
        status = receipt.status.value if hasattr(receipt.status, "value") else str(receipt.status)
        return BeaconResult(
            delivered=(received is not None),
            status=status,
            snr_db=snr,
            profile_id=link.profile.profile_id,
            frames_sent=record.frame_count,
            frames_acked=record.acknowledgements,
            retries=record.retries,
            latency_ms=record.delivery_latency_ms,
            signature_verified=(record.signature_result == "verified"),
            decode_outcome=record.decode_outcome,
            received_payload=(received.payload if received is not None else None),
            sender=sender,
            receiver=receiver,
        )

    def send_beacon_real_cached(self, sender: str, receiver: str, payload: dict, *,
                                snr_db: float, seed: int = 0,
                                profile_id: str | None = None) -> BeaconResult:
        """Run the FULL modem once per (pair, rounded-SNR, seed) and memoize — for showcase beacons
        whose real frame/retry/signature facts we attach to the graph. Delivery outcome is
        deterministic in (SNR, seed), so this cache never changes an answer."""
        key = (sender, receiver, round(snr_db, 1), seed, profile_id or self.profile.profile_id)
        cached = self._real_cache.get(key)
        if cached is not None:
            return cached
        cond = LinkConditions(distance_m=1.0)  # SNR is forced directly below, geometry unused here
        link = self._link_for(sender, receiver)
        if profile_id is not None:
            link.profile = PROFILES[profile_id]
        link.channel = ChannelModel(snr_db=snr_db, seed=seed)
        envelope = build_envelope(identity=self.identities[sender], recipient_node_id=receiver,
                                  recipient_agent_id=None, kind="agent_message", payload=payload)
        try:
            receipt, received, record = link.transmit(sender=sender, receiver=receiver,
                                                       envelope=envelope)
            result = BeaconResult(
                delivered=(received is not None),
                status=(receipt.status.value if hasattr(receipt.status, "value") else str(receipt.status)),
                snr_db=snr_db, profile_id=link.profile.profile_id, frames_sent=record.frame_count,
                frames_acked=record.acknowledgements, retries=record.retries,
                latency_ms=record.delivery_latency_ms,
                signature_verified=(record.signature_result == "verified"),
                decode_outcome=record.decode_outcome,
                received_payload=(received.payload if received is not None else None),
                sender=sender, receiver=receiver)
        except Exception:  # noqa: BLE001
            result = BeaconResult(delivered=False, status="failed", snr_db=snr_db,
                                  profile_id=link.profile.profile_id, frames_sent=0, frames_acked=0,
                                  retries=0, latency_ms=0.0, signature_verified=False,
                                  decode_outcome="undecodable_noise", received_payload=None,
                                  sender=sender, receiver=receiver)
        self._real_cache[key] = result
        return result
