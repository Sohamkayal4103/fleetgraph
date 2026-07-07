"""beta.3 OTA MAC / link-protocol tests (deterministic; no radio).

Covers the complete medium-access + link state machine: RTS/CTS handshake, lost RTS/CTS, CTS
rejection, lost/corrupted DATA, lost BLOCK_ACK, partial-bitmap selective retransmission,
duplicate suppression, cancellation, two-node TDD, three-node contention, simultaneous-RTS
collision, randomized backoff, hidden-node mitigation, fairness/no-starvation, wrong-mesh frame
rejection, MAC frame auth, and the link-ACK vs delivery-receipt vs mission-ack/result separation.
"""

from __future__ import annotations

import pytest

from aithernet.transport.ota.frame import FrameType
from aithernet.transport.ota.mac import (
    LinkState,
    LossPlan,
    MacConfig,
    MacEndpoint,
    MacError,
    MacFrame,
    MacMode,
    VirtualMedium,
    mesh_discriminator,
    reassemble_fragments,
    run_mac_transfer,
    short_address,
    simulate_contention,
)

KEY = b"k" * 32


def _endpoints(mesh="m1"):
    return (
        MacEndpoint("node-A", link_key=KEY, mesh_id=mesh),
        MacEndpoint("node-B", link_key=KEY, mesh_id=mesh),
    )


def _frags(n=5, size=8):
    return [bytes([i]) * size for i in range(n)]


# -- frame codec -----------------------------------------------------------------


def test_mac_frame_codec_roundtrip_and_all_types():
    for ft in (FrameType.BEACON, FrameType.RTS, FrameType.CTS, FrameType.DATA,
               FrameType.BLOCK_ACK, FrameType.NACK, FrameType.CANCEL, FrameType.FIN,
               FrameType.DELIVERY_RECEIPT):
        f = MacFrame(ft, 7, 1, 2, 99, 99, 0, 3, 1, 3, 1000, b"body")
        g = MacFrame.decode(f.encode(KEY), KEY)
        assert g.frame_type is ft
        assert g.payload == b"body"


def test_mac_frame_auth_rejects_wrong_key_and_corruption():
    raw = MacFrame(FrameType.DATA, 0, 1, 2, 1, 1, 0, 1, 0, 1, 0, b"hi").encode(KEY)
    with pytest.raises(MacError):
        MacFrame.decode(raw, b"x" * 32)
    corrupted = bytearray(raw)
    corrupted[20] ^= 0xFF
    with pytest.raises(MacError):
        MacFrame.decode(bytes(corrupted), KEY)


def test_short_address_and_mesh_discriminator_are_stable_and_nonzero():
    assert short_address("node-A") == short_address("node-A")
    assert short_address("node-A") != short_address("node-B")
    assert mesh_discriminator(None) == 0
    assert mesh_discriminator("m1") != 0


# -- happy paths -----------------------------------------------------------------


def test_rts_cts_shared_channel_success():
    a, b = _endpoints()
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(),
                         config=MacConfig(), seed=1)
    assert r.delivered
    assert r.final_state == LinkState.COMPLETE.value
    assert r.rts_sent == 1 and r.cts_received == 1
    assert r.reservation_us > 0
    assert r.link_acknowledged and r.delivery_receipt
    assert LinkState.CTS_GRANTED.value in r.states


def test_point_to_point_tdd_skips_rts_cts():
    a, b = _endpoints()
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(),
                         config=MacConfig(mode=MacMode.POINT_TO_POINT_TDD), seed=2)
    assert r.delivered and r.mac_mode == "point_to_point_tdd"
    assert r.rts_sent == 0 and r.reservation_us > 0


# -- loss / retransmission -------------------------------------------------------


def test_lost_rts_retried_with_backoff():
    a, b = _endpoints()
    lp = LossPlan(drop={(a.addr, FrameType.RTS, 1)})
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(lp),
                         config=MacConfig(), seed=3)
    assert r.delivered
    assert r.handshake_attempts >= 2
    assert r.backoff_events >= 1


def test_lost_cts_retried():
    a, b = _endpoints()
    lp = LossPlan(drop={(b.addr, FrameType.CTS, 1)})
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(lp),
                         config=MacConfig(), seed=4)
    assert r.delivered and r.handshake_attempts >= 2


def test_cts_rejection_fails_fast():
    a, b = _endpoints()
    # A reservation larger than the receiver will accept -> CTS rejected.
    cfg = MacConfig(cts_max_accept_frames=2)
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(5), medium=VirtualMedium(),
                         config=cfg, seed=5)
    assert not r.delivered
    assert r.cts_rejected == 1
    assert r.final_state == LinkState.FAILED.value


def test_lost_data_frame_is_selectively_retransmitted():
    a, b = _endpoints()
    lp = LossPlan(drop={(a.addr, FrameType.DATA, 2)})
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(lp),
                         config=MacConfig(), seed=6)
    assert r.delivered
    assert r.retransmissions >= 1
    assert r.retransmit_rounds >= 1
    assert r.nacks >= 1


def test_corrupted_data_frame_recovered_by_retransmit():
    a, b = _endpoints()
    lp = LossPlan(corrupt={(a.addr, FrameType.DATA, 3)})
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(lp),
                         config=MacConfig(), seed=7)
    assert r.delivered and r.retransmissions >= 1


def test_lost_block_ack_triggers_retransmit_round():
    a, b = _endpoints()
    lp = LossPlan(drop={(b.addr, FrameType.BLOCK_ACK, 1)})
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(lp),
                         config=MacConfig(), seed=8)
    assert r.delivered
    assert r.retransmit_rounds >= 1


def test_partial_bitmap_only_resends_missing_frames():
    a, b = _endpoints()
    # Drop two specific frames on the first burst; only those should be resent.
    lp = LossPlan(drop={(a.addr, FrameType.DATA, 2), (a.addr, FrameType.DATA, 4)})
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(6), medium=VirtualMedium(lp),
                         config=MacConfig(), seed=9)
    assert r.delivered
    # 6 in the first burst + exactly the 2 missing retransmitted = 8 data frames.
    assert r.data_frames_sent == 8
    assert r.retransmissions == 2


def test_unrecoverable_loss_fails_within_bounded_rounds():
    a, b = _endpoints()
    # Always drop frame index 0 (every occurrence) -> never recovers; must fail, not loop.
    drops = {(a.addr, FrameType.DATA, n) for n in range(1, 40)}
    lp = LossPlan(drop=drops)
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(3),
                         medium=VirtualMedium(lp), config=MacConfig(max_retransmit_rounds=3),
                         seed=10)
    assert not r.delivered
    assert r.final_state == LinkState.FAILED.value
    assert LinkState.FIN.value in r.states


def test_handshake_failure_within_attempt_budget():
    a, b = _endpoints()
    drops = {(a.addr, FrameType.RTS, n) for n in range(1, 20)}
    medium = VirtualMedium(LossPlan(drop=drops))
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=medium,
                         config=MacConfig(max_handshake_attempts=3), seed=11)
    assert not r.delivered
    assert r.handshake_attempts == 3


# -- cancellation ----------------------------------------------------------------


def test_cancellation_before_data_burst():
    a, b = _endpoints()
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(),
                         config=MacConfig(), seed=12, cancel_before_data=True)
    assert r.cancelled
    assert r.final_state == LinkState.CANCELLED.value
    assert not r.delivered


# -- fairness / contention -------------------------------------------------------


def test_three_node_contention_is_fair_with_no_starvation():
    o = simulate_contention(contenders=["A", "B", "C"], rounds=12, config=MacConfig(), seed=7)
    assert sum(o.per_node_wins.values()) == 12
    assert o.starved == []
    assert o.fairness_ok


def test_simultaneous_rts_produces_collisions_resolved_by_backoff():
    o = simulate_contention(contenders=["A", "B", "C", "D"], rounds=16, config=MacConfig(), seed=3)
    assert o.collisions >= 1
    assert o.backoff_redraws >= o.collisions
    assert o.starved == []


def test_hidden_node_collisions_detected_and_resolved():
    o = simulate_contention(contenders=["A", "B", "C"], rounds=12, config=MacConfig(), seed=0,
                            hidden_pairs={("A", "C")})
    assert o.hidden_node_collisions >= 1
    assert o.fairness_ok and o.starved == []


def test_fairness_bound_blocks_excess_consecutive_claims():
    # A node that already hit the consecutive-claim cap yields the channel on its next attempt.
    a, b = _endpoints()
    a.consecutive_claims = MacConfig().max_consecutive_claims
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(),
                         config=MacConfig(), seed=13)
    assert not r.delivered
    assert "fairness" in r.detail


# -- wrong mesh / cross-link rejection ------------------------------------------


def test_wrong_mesh_frame_is_distinguishable():
    a, b = _endpoints("m1")
    other = MacEndpoint("node-Z", link_key=KEY, mesh_id="m2")
    frame = MacFrame(FrameType.DATA, other.mesh_disc, other.addr, b.addr, 1, 1, 0, 1, 0, 1, 0, b"x")
    decoded = MacFrame.decode(frame.encode(KEY), KEY)
    # The receiver can see the frame is for a different mesh discriminator than its own.
    assert decoded.mesh_disc != b.mesh_disc
    assert decoded.mesh_disc == mesh_discriminator("m2")


# -- acknowledgement layers separated -------------------------------------------


def test_link_ack_and_delivery_receipt_are_distinct_layers():
    a, b = _endpoints()
    r = run_mac_transfer(tx=a, rx=b, payload_fragments=_frags(), medium=VirtualMedium(),
                         config=MacConfig(), seed=14)
    # A successful link exchange yields BOTH a link ACK and a delivery receipt, but they are
    # separate booleans — neither is a mission-level acknowledgement.
    assert r.link_acknowledged is True
    assert r.delivery_receipt is True
    # The MAC result carries no mission_ack / mission_result field — those belong to a higher layer.
    assert not hasattr(r, "mission_ack")


def test_reassemble_fragments_roundtrip():
    frags = {0: b"aa", 1: b"bb", 2: b"cc"}
    assert reassemble_fragments(frags, 3) == b"aabbcc"
    with pytest.raises(MacError):
        reassemble_fragments({0: b"aa"}, 3)
