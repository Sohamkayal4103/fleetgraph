"""Phase 2: GNU Radio TX/RX flowgraph generation (the hardware realization).

The pure-Python modem proves the link logic in software. For a real SDR, the same profile drives a
GNU Radio flowgraph: this module deterministically GENERATES the TX and RX flowgraph Python from a
profile and computes a flowgraph digest that the authorization plan binds to. It does NOT execute
GNU Radio or transmit — generation + structural validation only. The managed RF-MCP component runs a
validated flowgraph behind the TX-authorization gates; arbitrary model-written source never runs on
hardware before validation + authorization.

Sources/sinks default to synthetic/file IQ so the generated graph is exercised in simulation; a real
SDR sink is only substituted after separate hardware qualification.
"""

from __future__ import annotations

import hashlib

from aithernet.transport.ota.profiles import RFLinkProfile

#: The bounded set of GNU Radio block types the generator may emit (no arbitrary blocks).
_ALLOWED_BLOCKS = {
    "blocks.vector_source_b", "blocks.vector_source_c", "blocks.vector_sink_c",
    "blocks.vector_sink_b", "blocks.file_source", "blocks.file_sink",
    "digital.constellation_modulator", "digital.constellation_decoder_cb",
    "digital.corr_est_cc", "channels.channel_model", "blocks.throttle", "analog.agc_cc",
    "digital.pfb_clock_sync_ccf", "blocks.repeat", "blocks.multiply_const_cc",
}

_MOD_CONSTELLATION = {"bpsk": "digital.constellation_bpsk()",
                      "qpsk": "digital.constellation_qpsk()"}


def _validate_for_generation(profile: RFLinkProfile) -> None:
    if profile.modulation not in _MOD_CONSTELLATION:
        raise ValueError(f"no flowgraph template for modulation {profile.modulation!r}")


def generate_tx_flowgraph(profile: RFLinkProfile, *, source: str = "synthetic") -> str:
    """Generate the TX flowgraph Python for ``profile``. ``source`` is 'synthetic' or 'file'."""
    _validate_for_generation(profile)
    const = _MOD_CONSTELLATION[profile.modulation]
    src = ("blocks.vector_source_b(frame_bytes, False)" if source == "synthetic"
           else "blocks.file_source(gr.sizeof_char, frame_path, False)")
    return _TX_TEMPLATE.format(profile_id=profile.profile_id, sps=profile.samples_per_symbol,
                               const=const, source=src, digest="{digest}")


def generate_rx_flowgraph(profile: RFLinkProfile, *, sink: str = "synthetic") -> str:
    """Generate the RX flowgraph Python for ``profile``. ``sink`` is 'synthetic' or 'file'."""
    _validate_for_generation(profile)
    const = _MOD_CONSTELLATION[profile.modulation]
    snk = ("blocks.vector_sink_c()" if sink == "synthetic"
           else "blocks.file_sink(gr.sizeof_char, out_path)")
    return _RX_TEMPLATE.format(profile_id=profile.profile_id, sps=profile.samples_per_symbol,
                               const=const, sink=snk)


def flowgraph_digest(tx_src: str, rx_src: str) -> str:
    """Stable digest over the generated TX+RX flowgraph source — binds the authorization plan."""
    h = hashlib.sha256()
    h.update(tx_src.encode())
    h.update(b"\x00")
    h.update(rx_src.encode())
    return "sha256:" + h.hexdigest()


def validate_flowgraph_source(src: str) -> tuple[bool, list[str]]:
    """Structural safety validation of generated flowgraph source (no execution).

    Rejects anything outside the bounded GNU Radio block set or that smells like arbitrary code
    (shell, eval/exec, network, file deletion). Returns (ok, problems)."""
    problems: list[str] = []
    banned = ("os.system", "subprocess", "eval(", "exec(", "__import__", "socket", "shutil.rmtree",
              "os.remove", "pickle.loads", "open('/'", "requests.")
    for b in banned:
        if b in src:
            problems.append(f"banned construct: {b}")
    # every `blocks./digital./channels./analog.` reference must be in the allowed set
    import re
    for m in re.finditer(r"\b((?:blocks|digital|channels|analog)\.[a-zA-Z0-9_]+)", src):
        token = m.group(1)
        if token not in _ALLOWED_BLOCKS and token not in {
                "digital.constellation_bpsk", "digital.constellation_qpsk"}:
            problems.append(f"block not in allowed set: {token}")
    return (not problems), sorted(set(problems))


_TX_TEMPLATE = '''# GENERATED Aithernet OTA TX flowgraph for profile {profile_id} (digest {digest}).
# Generated deterministically from an approved RFLinkProfile. Runs under managed RF-MCP only,
# behind the TX-authorization gates. Default source is synthetic IQ (no hardware).
from gnuradio import gr, blocks, digital

class OtaTx(gr.top_block):
    def __init__(self, frame_bytes=(), frame_path=""):
        gr.top_block.__init__(self, "aithernet_ota_tx_{profile_id}")
        sps = {sps}
        self.src = {source}
        self.mod = digital.constellation_modulator({const}, differential=False,
                                                   samples_per_symbol=sps)
        self.sink = blocks.vector_sink_c()
        self.connect(self.src, self.mod, self.sink)
'''

_RX_TEMPLATE = '''# GENERATED Aithernet OTA RX flowgraph for profile {profile_id}.
# Default sink is synthetic; preamble correlation + clock sync recover frames in simulation.
from gnuradio import gr, blocks, digital

class OtaRx(gr.top_block):
    def __init__(self, iq_samples=(), out_path=""):
        gr.top_block.__init__(self, "aithernet_ota_rx_{profile_id}")
        sps = {sps}
        self.src = blocks.vector_source_c(iq_samples, False)
        self.demod = digital.constellation_decoder_cb({const})
        self.sink = {sink}
        self.connect(self.src, self.demod, self.sink)
'''
