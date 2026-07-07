"""Deterministic canonical JSON serialization for signed transport messages (Stage 13A).

The signature of an envelope (or acknowledgement, or manifest) covers a canonical
serialization of every authenticated field EXCEPT the signature itself. Both signer and
verifier must produce byte-identical output, so we never sign a Python ``repr`` or an
unordered ``json.dumps``.

Canonicalization format (documented contract):
  * the authenticated object is a JSON object with the ``signature`` key removed;
  * keys are sorted lexicographically at every level (``sort_keys=True``);
  * the most compact separators are used (``","`` and ``":"`` — no incidental whitespace);
  * non-ASCII is preserved (``ensure_ascii=False``) and the result is UTF-8 encoded;
  * ``NaN``/``Infinity`` are rejected (``allow_nan=False``) so output stays valid JSON.

Because object key order in JSON is not semantically significant, two senders that build
the same logical object in different field orders canonicalize to identical bytes.
"""

from __future__ import annotations

import hashlib
import json

#: The single field excluded from the authenticated, canonicalized form.
SIGNATURE_FIELD = "signature"


def canonical_bytes(obj: dict) -> bytes:
    """Return the deterministic canonical UTF-8 bytes of ``obj`` (excluding the signature).

    The ``signature`` key, if present, is removed before serialization so the same call is
    used to produce signing input and to re-derive verification input.
    """
    authenticated = {key: value for key, value in obj.items() if key != SIGNATURE_FIELD}
    return json.dumps(
        authenticated,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(obj: dict) -> str:
    """Return ``sha256:<hex>`` over the canonical bytes of ``obj`` (excluding the signature)."""
    digest = hashlib.sha256(canonical_bytes(obj)).hexdigest()
    return f"sha256:{digest}"
