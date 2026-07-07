"""1.0.0-beta.1 OTA: deterministic transport selection + RF dataset classes."""

from __future__ import annotations

import pytest

from aithernet.data.dataset_builder import DATASET_CLASSES
from aithernet.transport.ota import selection as sel


def test_auto_prefers_ip_then_sim_then_rf():
    r = sel.select_transport(requested="auto",
                             available={"ip": True, "simulated_rf": True, "rf_ota": True},
                             fallback_allowed=False)
    assert r.selected == "ip" and not r.fallback_occurred
    r2 = sel.select_transport(requested="auto",
                              available={"ip": False, "simulated_rf": True, "rf_ota": True},
                              fallback_allowed=False)
    assert r2.selected == "simulated_rf"


def test_explicit_request_satisfied():
    r = sel.select_transport(requested="simulated_rf",
                             available={"ip": True, "simulated_rf": True}, fallback_allowed=False)
    assert r.selected == "simulated_rf" and not r.fallback_occurred


def test_rf_not_silently_downgraded_to_ip():
    # rf_ota requested but unavailable, fallback NOT allowed -> error (no silent IP downgrade)
    with pytest.raises(sel.TransportSelectionError):
        sel.select_transport(requested="rf_ota", available={"ip": True, "rf_ota": False},
                             fallback_allowed=False)


def test_explicit_fallback_is_recorded():
    r = sel.select_transport(requested="rf_ota", available={"ip": True, "rf_ota": False},
                             fallback_allowed=True)
    assert r.selected == "ip" and r.fallback_occurred is True
    assert "fallback" in r.reason


def test_rf_dataset_classes_present():
    for cls in ("rf-transport-routing", "modem-profile-selection", "frame-recovery",
                "link-adaptation", "delivery-prediction", "retransmission-policy",
                "snr-error-analysis", "generated-modem-validation", "peer-message-delivery",
                "simulated-channel-outcomes"):
        assert cls in DATASET_CLASSES
