"""Stage 13A identity tests (Part T items 1-8)."""

from __future__ import annotations

import json
import stat

from aithernet.transport.identity import (
    IdentityManager,
    fingerprint_for_public_key,
    verify_signature,
)


def _mgr(tmp_path, node_id="node-A", name="A") -> IdentityManager:
    return IdentityManager(tmp_path / "id", node_id=node_id, node_name=name)


def test_identity_creation(tmp_path) -> None:
    ident = _mgr(tmp_path).initialize()
    assert ident.node_id == "node-A"
    assert ident.public_key_b64 and ident.fingerprint.startswith("sha256:")


def test_idempotent_initialization(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    first = mgr.initialize()
    second = mgr.initialize()  # no rotate -> same key
    assert first.public_key_b64 == second.public_key_b64
    assert first.fingerprint == second.fingerprint


def test_restrictive_key_file_permissions(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    mgr.initialize()
    mode = stat.S_IMODE(mgr.private_key_path.stat().st_mode)
    assert mode == 0o600


def test_stable_fingerprint(tmp_path) -> None:
    ident = _mgr(tmp_path).initialize()
    assert fingerprint_for_public_key(ident.public_key_b64) == ident.fingerprint


def test_signing_and_verification(tmp_path) -> None:
    ident = _mgr(tmp_path).initialize()
    sig = ident.sign(b"payload-bytes")
    assert verify_signature(ident.public_key_b64, b"payload-bytes", sig)


def test_invalid_signature_rejected(tmp_path) -> None:
    ident = _mgr(tmp_path).initialize()
    sig = ident.sign(b"payload-bytes")
    assert not verify_signature(ident.public_key_b64, b"tampered", sig)
    assert not verify_signature(ident.public_key_b64, b"payload-bytes", "bm90LWEtc2ln")


def test_private_key_absent_from_public_document(tmp_path) -> None:
    ident = _mgr(tmp_path).initialize()
    doc = ident.public_document()
    blob = json.dumps(doc)
    assert "PRIVATE" not in blob
    assert "private" not in {k.lower() for k in doc}
    # The on-disk public doc never contains private material either.
    public_doc = json.loads(_mgr(tmp_path).public_doc_path.read_text())
    assert "private" not in json.dumps(public_doc).lower()


def test_identity_survives_restart(tmp_path) -> None:
    first = _mgr(tmp_path).initialize()
    # A fresh manager over the same directory loads the same identity (simulated restart).
    reloaded = _mgr(tmp_path).load()
    assert reloaded.public_key_b64 == first.public_key_b64
    assert reloaded.fingerprint == first.fingerprint
    # And it can still sign verifiably.
    sig = reloaded.sign(b"after-restart")
    assert verify_signature(first.public_key_b64, b"after-restart", sig)
