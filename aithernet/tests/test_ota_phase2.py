"""1.0.0-beta.1 OTA Phase 2: reference modem + FEC + impaired channel + stop-and-wait ARQ.

Pure software; no radio. Proves modulate->channel->demodulate recovers frames through AWGN + phase
offset + leading delay, that FEC recovers within limits and fails beyond them, and that the ARQ
layer delivers with retransmission + duplicate suppression.
"""

from __future__ import annotations

import os

import pytest

from aithernet.transport.ota import channel as ch
from aithernet.transport.ota import fec, modem
from aithernet.transport.ota.frame import Frame, FrameType, fragment
from aithernet.transport.ota.profiles import REFERENCE_BPSK
from aithernet.transport.ota.reliability import Receiver, send_message

# -- FEC ------------------------------------------------------------------------------------

def test_fec_repetition3_corrects_single_bit_errors():
    bits = fec.bytes_to_bits(b"hi")
    coded = fec.fec_encode(bits, "repetition3")
    coded[0] ^= 1  # flip one of the three copies
    assert fec.fec_decode(coded, "repetition3") == bits


def test_fec_hamming74_corrects_single_bit_per_block():
    bits = fec.bytes_to_bits(b"AB")
    coded = fec.fec_encode(bits, "hamming74")
    coded[2] ^= 1  # single-bit error in the first block
    assert fec.fec_decode(coded, "hamming74")[: len(bits)] == bits


def test_bytes_bits_roundtrip():
    data = os.urandom(33)
    assert fec.bits_to_bytes(fec.bytes_to_bits(data)) == data


# -- modem + channel --------------------------------------------------------------------------

def _frame(payload):
    return Frame(FrameType.DATA, msg_id=99, seq=0, total=1, payload=payload).encode("crc16")


def test_modem_clean_channel_recovers_frame():
    raw = _frame(b"canonical-peer-frame")
    samples = modem.modulate(raw, REFERENCE_BPSK)
    out = modem.demodulate(samples, REFERENCE_BPSK)
    decoded = Frame.decode(out.data, "crc16")
    assert decoded.payload == b"canonical-peer-frame"


def test_modem_recovers_through_awgn_phase_and_delay():
    raw = _frame(b"through the impaired channel")
    samples = modem.modulate(raw, REFERENCE_BPSK)
    impaired = ch.apply_channel(samples, ch.ChannelModel(
        snr_db=12.0, phase_rad=0.9, delay_samples=37, seed=1))
    out = modem.demodulate(impaired, REFERENCE_BPSK)
    assert Frame.decode(out.data, "crc16").payload == b"through the impaired channel"


def test_fec_recovers_at_low_snr_within_limit():
    raw = _frame(b"low snr but rep3 saves it")
    samples = modem.modulate(raw, REFERENCE_BPSK)
    impaired = ch.apply_channel(samples, ch.ChannelModel(snr_db=6.0, phase_rad=0.2, seed=3))
    out = modem.demodulate(impaired, REFERENCE_BPSK)
    assert Frame.decode(out.data, "crc16").payload == b"low snr but rep3 saves it"


def test_failure_beyond_fec_limit_is_detected_by_crc():
    raw = _frame(b"too much noise to recover")
    samples = modem.modulate(raw, REFERENCE_BPSK)
    impaired = ch.apply_channel(samples, ch.ChannelModel(snr_db=-6.0, phase_rad=0.0, seed=5))
    out = modem.demodulate(impaired, REFERENCE_BPSK)
    # Beyond the FEC limit the CRC must catch the corruption (never a silent wrong frame).
    from aithernet.transport.ota.frame import FrameError
    with pytest.raises((FrameError, ValueError)):
        Frame.decode(out.data, "crc16")


def test_fragmentation_reassembly_under_modem():
    payload = os.urandom(300)
    frames = fragment(payload, msg_id=5, max_frame_payload=128)
    rx = Receiver(REFERENCE_BPSK)
    completed = None
    for f in frames:
        samples = modem.modulate(f.encode("crc16"), REFERENCE_BPSK)
        impaired = ch.apply_channel(samples, ch.ChannelModel(snr_db=15.0, seed=f.seq))
        out = modem.demodulate(impaired, REFERENCE_BPSK)
        _ack, msg = rx.on_frame(out.data)
        if msg is not None:
            completed = msg
    assert completed == payload


# -- ARQ: retransmission + duplicate suppression ----------------------------------------------

def test_arq_retransmits_then_delivers():
    rx = Receiver(REFERENCE_BPSK)
    attempts = {"n": 0}

    def channel(encoded: bytes):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None                    # first frame "lost" -> forces a retransmit
        ack, _msg = rx.on_frame(encoded)
        return ack

    stats = send_message(b"reliable payload", msg_id=11, profile=REFERENCE_BPSK, channel=channel)
    assert stats.delivered is True and stats.retries >= 1


def test_arq_duplicate_fragments_suppressed():
    rx = Receiver(REFERENCE_BPSK)
    f = fragment(b"x" * 10, msg_id=22, max_frame_payload=128)[0].encode("crc16")
    ack1, msg1 = rx.on_frame(f)
    ack2, msg2 = rx.on_frame(f)            # duplicate
    assert msg1 == b"x" * 10
    assert msg2 is None                    # duplicate suppressed, but still ACKed
    assert ack2 is not None
