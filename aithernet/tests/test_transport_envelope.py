"""Stage 13A canonical-envelope + manifest tests (Part T items 9-16, 52-56)."""

from __future__ import annotations

import json

import pytest

from aithernet.transport.canonical import canonical_bytes, canonical_hash
from aithernet.transport.envelope import (
    MessageKind,
    build_envelope,
    parse_envelope,
    validate_recipient,
    validate_timing,
)
from aithernet.transport.errors import (
    EnvelopeError,
    ExpiredMessageError,
    PayloadTooLargeError,
    ProtocolVersionError,
    RecipientMismatchError,
)
from aithernet.transport.identity import IdentityManager
from aithernet.transport.manifest import (
    CAP_AGENT_TRANSPORT,
    build_manifest,
    verify_manifest,
)


def _ident(tmp_path, node_id="node-A"):
    return IdentityManager(tmp_path / node_id, node_id=node_id, node_name=node_id).initialize()


def _envelope(identity, **kw):
    return build_envelope(
        identity=identity, recipient_node_id=kw.pop("recipient", "node-B"),
        recipient_agent_id=None, kind=MessageKind.AGENT_MESSAGE.value,
        payload=kw.pop("payload", {"subject": "s", "text": "t"}), **kw,
    )


# 9 / 10 -- canonicalization ------------------------------------------------------


def test_deterministic_canonical_json() -> None:
    obj = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
    assert canonical_bytes(obj) == b'{"a":2,"b":1,"nested":{"x":2,"y":1}}'


def test_field_order_independence(tmp_path) -> None:
    env = _envelope(_ident(tmp_path))
    raw = json.loads(env.model_dump_json())
    shuffled = dict(reversed(list(raw.items())))
    assert canonical_hash(raw) == canonical_hash(shuffled)


# 11 -- mutation invalidates signature -------------------------------------------


def test_signed_field_mutation_invalidates_signature(tmp_path) -> None:
    ident = _ident(tmp_path)
    env = _envelope(ident)
    raw = json.loads(env.model_dump_json())
    assert parse_envelope(raw, maximum_payload_bytes=1 << 20).verify_signature_with(
        ident.public_key_b64
    )
    raw["payload"] = {"subject": "s", "text": "TAMPERED"}
    assert not parse_envelope(raw, maximum_payload_bytes=1 << 20).verify_signature_with(
        ident.public_key_b64
    )


# 12 -- unsupported protocol version ---------------------------------------------


def test_unsupported_protocol_version_rejected(tmp_path) -> None:
    env = _envelope(_ident(tmp_path))
    raw = json.loads(env.model_dump_json())
    raw["protocol_version"] = "99"
    with pytest.raises(ProtocolVersionError):
        parse_envelope(raw, maximum_payload_bytes=1 << 20)
    raw2 = json.loads(env.model_dump_json())
    raw2["protocol"] = "something-else"
    with pytest.raises(ProtocolVersionError):
        parse_envelope(raw2, maximum_payload_bytes=1 << 20)


# 13 / 14 -- timing --------------------------------------------------------------


def test_expired_message_rejected(tmp_path) -> None:
    env = _envelope(_ident(tmp_path), expires_in_seconds=-10)  # already expired
    with pytest.raises(ExpiredMessageError):
        validate_timing(env, accepted_clock_skew_seconds=1)


def test_excessive_clock_skew_rejected(tmp_path) -> None:
    from datetime import timedelta

    from aithernet.state.models import utcnow

    env = _envelope(_ident(tmp_path))
    env = env.model_copy(update={"created_at": (utcnow() + timedelta(hours=1)).isoformat()})
    with pytest.raises(ExpiredMessageError):
        validate_timing(env, accepted_clock_skew_seconds=60)


# 15 -- recipient mismatch -------------------------------------------------------


def test_recipient_mismatch_rejected(tmp_path) -> None:
    env = _envelope(_ident(tmp_path), recipient="node-B")
    validate_recipient(env, local_node_id="node-B")  # ok
    with pytest.raises(RecipientMismatchError):
        validate_recipient(env, local_node_id="node-C")


# 16 -- payload size limit -------------------------------------------------------


def test_payload_size_limit_enforced(tmp_path) -> None:
    env = _envelope(_ident(tmp_path), payload={"blob": "x" * 5000})
    raw = json.loads(env.model_dump_json())
    with pytest.raises(PayloadTooLargeError):
        parse_envelope(raw, maximum_payload_bytes=1024)


def test_missing_signature_rejected(tmp_path) -> None:
    env = _envelope(_ident(tmp_path))
    raw = json.loads(env.model_dump_json())
    raw["signature"] = None
    with pytest.raises(EnvelopeError):
        parse_envelope(raw, maximum_payload_bytes=1 << 20)


# 52-56 -- capability manifest ---------------------------------------------------


def test_signed_manifest_produced_and_verified(tmp_path) -> None:
    ident = _ident(tmp_path)
    manifest = build_manifest(ident, node_name="A", endpoint="http://a", capabilities=[
        CAP_AGENT_TRANSPORT
    ])
    assert manifest["signature"]
    ok, reason = verify_manifest(manifest, public_key_b64=ident.public_key_b64)
    assert ok, reason


def test_tampered_manifest_rejected(tmp_path) -> None:
    ident = _ident(tmp_path)
    manifest = build_manifest(ident, node_name="A", endpoint="http://a", capabilities=[
        CAP_AGENT_TRANSPORT
    ])
    manifest["capabilities"] = ["mission_acceptance", "evil"]
    ok, _ = verify_manifest(manifest, public_key_b64=ident.public_key_b64)
    assert not ok


def test_manifest_is_compact_and_secret_free(tmp_path) -> None:
    ident = _ident(tmp_path)
    manifest = build_manifest(ident, node_name="A", endpoint="http://a", capabilities=[
        CAP_AGENT_TRANSPORT
    ])
    blob = json.dumps(manifest).lower()
    # High-level capabilities only — never tool schemas, catalogs, paths, env, or secrets.
    for forbidden in ("inputschema", "database", "workspace", "api_key", "token",
                      "password", "/home/", "env", "private"):
        assert forbidden not in blob
    assert isinstance(manifest["capabilities"], list)
    assert manifest["supported_kinds"]  # compact kind list
