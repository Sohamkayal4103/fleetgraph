"""Release manifest signing + verification (Stage 14F §23).

Detached Ed25519 signatures over a canonical release manifest, using the established
``cryptography`` Ed25519 implementation (no invented crypto, and NOT reusing node identity or
webhook keys). The production private signing key is never committed — it is loaded from an
env-referenced secret. Public verification keys ship with the installer. Temporary keys are
generated for tests. Verification is also exposed as a standalone command (see the hosted CLI).
"""

from __future__ import annotations

import hashlib
import json

from services.control_plane.security import generate_keypair, sign, verify_signature
from services.control_plane.security import load_private_key as _load_seed


def generate_release_keypair() -> tuple[str, str]:
    """Return ``(private_seed_b64, public_key_b64)`` — operator-provisioned, never committed."""
    return generate_keypair()


#: The set of artifact kinds the release system understands (manifest + portal + verify).
RELEASE_ARTIFACT_KINDS = (
    "deb", "wheel", "sdist", "manifest", "checksums", "signature", "pubkey",
    "sbom", "notices", "component-bundle", "component-source", "offline-bundle",
    "installer", "docs", "metadata",
)


def infer_kind(name: str) -> str:
    """Infer an artifact's kind from its filename (so artifacts are not all labelled ``wheel``).

    Deterministic and order-sensitive (most specific first). Unknown names fall back to
    ``metadata`` rather than a misleading ``wheel``.
    """
    n = (name or "").strip()
    low = n.lower()
    if low.endswith(".deb"):
        return "deb"
    if low.endswith(".whl"):
        return "wheel"
    if low.endswith(".sig"):
        return "signature"
    if low.endswith(".pub") or low.endswith(".pem"):
        return "pubkey"
    if low == "sha256sums" or low.endswith(".sha256") or low.endswith(".sha256sums"):
        return "checksums"
    if low == "manifest.json":
        return "manifest"
    if "sbom" in low or low.endswith(".spdx") or low.endswith(".spdx.json"):
        return "sbom"
    if "third-party-notices" in low or low == "notice.md" or low.endswith("notices.md"):
        return "notices"
    if "offline-bundle" in low or low.endswith("-offline.tar.gz"):
        return "offline-bundle"
    if low.endswith("-src.tar.gz") or "component-bundle" in low or "component-source" in low:
        return "component-source"
    if "install" in low and (low.endswith(".sh") or low.endswith(".md")):
        return "installer"
    if low.endswith(".tar.gz") or low.endswith(".tar.zst") or low.endswith(".zip"):
        return "sdist"
    if low.endswith(".md") or low.endswith(".txt") or low.endswith(".rst"):
        return "docs"
    return "metadata"


def public_key_pem(public_key_b64: str) -> str:
    """Convert a base64 raw Ed25519 public key into a PEM (SubjectPublicKeyInfo) string.

    Lets a customer verify the detached manifest signature with stock ``openssl pkeyutl -verify``
    and lets ``aithernet release verify`` use the same PEM the website documents.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def canonical_manifest(
    *, version: str, channel: str, signing_key_id: str, artifacts: list[dict],
    minimum_supported_version: str | None = None, notes_digest: str | None = None,
    schema_compat: str | None = None, os_support: list[str] | None = None,
    architecture: list[str] | None = None,
) -> dict:
    """A deterministic, fully-ordered manifest. Artifacts carry name/kind/sha256/size only."""
    norm_artifacts = sorted(
        (
            {
                "name": a["name"], "kind": a.get("kind", "wheel"),
                "architecture": a.get("architecture", "any"),
                "os_family": a.get("os_family", "any"),
                "sha256": a["sha256"], "byte_size": int(a.get("byte_size", 0)),
            }
            for a in artifacts
        ),
        key=lambda a: a["name"],
    )
    return {
        "version": version,
        "channel": channel,
        "signing_key_id": signing_key_id,
        "minimum_supported_version": minimum_supported_version,
        "schema_compat": schema_compat,
        "os_support": sorted(os_support or []),
        "architecture": sorted(architecture or []),
        "notes_digest": notes_digest,
        "artifacts": norm_artifacts,
    }


def manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_digest(manifest: dict) -> str:
    return "sha256:" + hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def sign_manifest(private_seed_b64: str, manifest: dict) -> str:
    private = _load_seed(private_seed_b64)
    return sign(private, manifest_bytes(manifest))


def verify_manifest(public_key_b64: str, manifest: dict, signature_b64: str) -> bool:
    return verify_signature(public_key_b64, manifest_bytes(manifest), signature_b64)


def verify_artifact(data: bytes, expected_sha256: str) -> bool:
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    return actual == expected_sha256
