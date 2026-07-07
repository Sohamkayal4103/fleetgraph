"""Immutable export-batch sealing + verification (Stage 14E, Part 11).

A batch is sealed once: record envelopes → canonical manifest + records → gzip → optional
AES-256-GCM authenticated encryption. Digests bind the manifest and records; the bundle is
never mutated after sealing. The ingestion side re-verifies the records digest after a
bomb-bounded decompression. Established formats only (gzip + cryptography AES-GCM); no invented
cryptography and no unbounded in-memory buffering beyond the configured batch byte cap.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass

from aithernet.data import crypto


def canonical_bytes(obj) -> bytes:
    """Deterministic JSON bytes for any JSON value (sorted keys, compact, UTF-8)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


BATCH_SCHEMA_VERSION = "1"
#: Hard cap on decompressed batch size (decompression-bomb protection).
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_BUNDLE_MAGIC = "aith-batch-enc.v1"


@dataclass
class SealedBatch:
    bundle: bytes
    manifest: dict
    manifest_digest: str
    records_digest: str
    uncompressed_bytes: int
    compressed_bytes: int
    encryption_metadata: dict | None


class BatchError(Exception):
    code = "batch_error"


def seal_batch(
    *,
    batch_id: str,
    tenant_id: str,
    node_pseudonym: str,
    destination_id: str,
    idempotency_key: str,
    record_envelopes: list[dict],
    consent_summary: dict,
    retention_summary: dict,
    created_at_iso: str,
    encryption_key_b64: str | None = None,
) -> SealedBatch:
    records_blob = canonical_bytes(record_envelopes)
    records_digest = crypto.sha256_hex(records_blob)
    manifest = {
        "batch_id": batch_id,
        "schema_version": BATCH_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "node_pseudonym": node_pseudonym,
        "destination_id": destination_id,
        "idempotency_key": idempotency_key,
        "created_at": created_at_iso,
        "record_count": len(record_envelopes),
        "records_digest": records_digest,
        "consent_summary": consent_summary,
        "retention_summary": retention_summary,
    }
    manifest_digest = crypto.sha256_hex(canonical_bytes(manifest))

    payload = {"manifest": manifest, "records": record_envelopes}
    uncompressed = canonical_bytes(payload)
    compressed = gzip.compress(uncompressed)

    if encryption_key_b64:
        env = crypto.encrypt(compressed, encryption_key_b64, aad=manifest_digest.encode())
        bundle = json.dumps({
            "magic": _BUNDLE_MAGIC,
            "scheme": env.version,
            "nonce_b64": env.nonce_b64,
            "aad_b64": env.aad_b64,
            "ciphertext_b64": _b64(env.ciphertext),
        }).encode("utf-8")
        encryption_metadata = env.metadata()
    else:
        bundle = compressed
        encryption_metadata = None

    return SealedBatch(
        bundle=bundle, manifest=manifest, manifest_digest=manifest_digest,
        records_digest=records_digest, uncompressed_bytes=len(uncompressed),
        compressed_bytes=len(compressed), encryption_metadata=encryption_metadata,
    )


def open_bundle(
    bundle: bytes, *, encryption_key_b64: str | None = None,
    max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
) -> tuple[dict, list[dict]]:
    """Decrypt (if needed) + bomb-bounded decompress + verify. Returns ``(manifest, records)``."""
    compressed = bundle
    if _looks_encrypted(bundle):
        if not encryption_key_b64:
            raise BatchError("bundle is encrypted but no key was supplied")
        try:
            obj = json.loads(bundle)
            env = crypto.EncryptionEnvelope(
                version=obj["scheme"], nonce_b64=obj["nonce_b64"],
                ciphertext=_unb64(obj["ciphertext_b64"]), aad_b64=obj.get("aad_b64"),
            )
            compressed = crypto.decrypt(env, encryption_key_b64)
        except BatchError:
            raise
        except Exception as exc:  # noqa: BLE001 — deterministic failure, no traceback leak
            raise BatchError("decryption failed") from exc

    uncompressed = _bounded_gunzip(compressed, max_uncompressed)
    try:
        payload = json.loads(uncompressed)
        manifest = payload["manifest"]
        records = payload["records"]
    except Exception as exc:  # noqa: BLE001
        raise BatchError("malformed batch payload") from exc

    expected = manifest.get("records_digest")
    actual = crypto.sha256_hex(canonical_bytes(records))
    if expected != actual:
        raise BatchError("records digest mismatch")
    if manifest.get("record_count") != len(records):
        raise BatchError("record count mismatch")
    return manifest, records


def verify_manifest_digest(manifest: dict, expected_digest: str) -> bool:
    return crypto.sha256_hex(canonical_bytes(manifest)) == expected_digest


# -- helpers --------------------------------------------------------------------


def _looks_encrypted(bundle: bytes) -> bool:
    return bundle[:1] == b"{" and _BUNDLE_MAGIC.encode() in bundle[:64]


def _bounded_gunzip(data: bytes, limit: int) -> bytes:
    import zlib

    dec = zlib.decompressobj(wbits=31)  # gzip
    out = bytearray()
    try:
        out.extend(dec.decompress(data, limit + 1))
        if len(out) > limit:
            raise BatchError("decompressed size exceeds bound")
        out.extend(dec.flush())
    except BatchError:
        raise
    except zlib.error as exc:
        raise BatchError("corrupt or non-gzip bundle") from exc
    if len(out) > limit:
        raise BatchError("decompressed size exceeds bound")
    return bytes(out)


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    import base64

    return base64.b64decode(text)
