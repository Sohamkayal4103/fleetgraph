"""Signed, compact capability manifest (Stage 13A, Part L).

A node exposes a small signed document advertising its identity, transport endpoint,
supported transport message kinds, and a HIGH-LEVEL capability list. The manifest is
strictly compact: it never contains tool schemas, credentials, the MCP catalog, private
configuration, database/workspace paths, environment, or prompts. A manifest NEVER creates
trust — a key in a manifest that differs from a peer's configured trusted key is rejected,
not adopted.
"""

from __future__ import annotations

from datetime import timedelta

from aithernet import __version__
from aithernet.state.models import utcnow
from aithernet.transport.canonical import canonical_bytes
from aithernet.transport.envelope import PROTOCOL, PROTOCOL_VERSION, MessageKind
from aithernet.transport.identity import NodeIdentity, verify_signature

#: High-level capability tokens (no tool schemas / catalogs).
CAP_MISSION_ACCEPTANCE = "mission_acceptance"
CAP_COORDINATOR_REASONING = "coordinator_reasoning"
CAP_CODING_AGENT = "coding_agent"
CAP_GNURADIO_MCP = "gnuradio_mcp"
CAP_AGENT_TRANSPORT = "agent_transport"

#: The transport message kinds this node supports.
SUPPORTED_KINDS = sorted(k.value for k in MessageKind)

#: Default manifest validity window.
_MANIFEST_TTL_SECONDS = 3600.0


def build_manifest(
    identity: NodeIdentity,
    *,
    node_name: str,
    endpoint: str | None,
    capabilities: list[str],
    ttl_seconds: float = _MANIFEST_TTL_SECONDS,
) -> dict:
    """Construct and SIGN a compact capability manifest from this node's identity."""
    now = utcnow()
    manifest = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "kind": MessageKind.CAPABILITY_MANIFEST.value,
        "node_id": identity.node_id,
        "node_name": node_name,
        "software_version": __version__,
        "public_key": identity.public_key_b64,
        "fingerprint": identity.fingerprint,
        "endpoint": endpoint,
        "supported_kinds": list(SUPPORTED_KINDS),
        "capabilities": sorted(set(capabilities)),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    manifest["signature"] = identity.sign(canonical_bytes(manifest))
    return manifest


def verify_manifest(manifest: object, *, public_key_b64: str) -> tuple[bool, str]:
    """Verify a manifest's signature against ``public_key_b64``. Returns ``(ok, reason)``.

    The caller is responsible for confirming the manifest's key/fingerprint matches the
    peer's CONFIGURED trusted key — a manifest never grants trust on its own.
    """
    if not isinstance(manifest, dict):
        return False, "manifest_not_object"
    if (
        manifest.get("protocol") != PROTOCOL
        or str(manifest.get("protocol_version")) != PROTOCOL_VERSION
    ):
        return False, "manifest_protocol_mismatch"
    signature = manifest.get("signature")
    if not isinstance(signature, str) or not signature:
        return False, "manifest_unsigned"
    if not verify_signature(public_key_b64, canonical_bytes(manifest), signature):
        return False, "manifest_bad_signature"
    return True, "ok"
