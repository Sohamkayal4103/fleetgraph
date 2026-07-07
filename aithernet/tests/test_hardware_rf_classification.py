"""Hardware classification truthfulness (beta.8 phase 7).

Soapy `audio` endpoints (WSL/RDP) and other non-RF factories must never be counted as physical RF
SDRs by ANY discovery surface; a node with no SDR reports zero devices.
"""

from __future__ import annotations

from aithernet.hardware import probing, setup

# Real-world SoapySDRUtil --find output observed on the beta.7 WSL node (two audio endpoints).
_AUDIO_ONLY = """\
Found device 0
  driver = audio
  label = RDP Source

Found device 1
  driver = audio
  label = Monitor of RDP Sink
"""

_PLUTO = """\
Found device 0
  driver = plutosdr
  label = PlutoSDR #0
  device = PlutoSDR
  uri = ip:192.168.2.1
"""

_MIXED = _PLUTO + "\n" + _AUDIO_ONLY


def test_is_rf_driver():
    assert probing.is_rf_driver("plutosdr")
    assert probing.is_rf_driver("uhd")
    assert not probing.is_rf_driver("audio")
    assert not probing.is_rf_driver("null")
    assert not probing.is_rf_driver(None)


def test_audio_endpoints_excluded_from_descriptors():
    descriptors = probing.soapy_find_to_descriptors("soapy", _AUDIO_ONLY)
    assert descriptors == []   # zero RF devices


def test_real_sdr_kept_audio_dropped():
    descriptors = probing.soapy_find_to_descriptors("soapy", _MIXED)
    assert len(descriptors) == 1
    assert descriptors[0]["driver"] == "plutosdr"


def test_legacy_discover_sdrs_excludes_audio(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda _x: "/usr/bin/SoapySDRUtil")
    monkeypatch.setattr(setup, "_cmd", lambda *a, **k: (0, _AUDIO_ONLY))
    assert setup.discover_sdrs() == []


def test_legacy_discover_sdrs_keeps_real_sdr(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda _x: "/usr/bin/SoapySDRUtil")
    monkeypatch.setattr(setup, "_cmd", lambda *a, **k: (0, _MIXED))
    found = setup.discover_sdrs()
    assert len(found) == 1 and found[0]["driver"] == "plutosdr"
