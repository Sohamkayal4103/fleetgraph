"""Content-addressed blob store for sealed export bundles (Stage 14E).

Sealed batch bytes never live in a relational column. They are written here — content-addressed,
confined under an operator-configured root, atomically finalized — and referenced from the
batch row by a store-relative path. Mirrors the existing artifact-store safety posture.
"""

from __future__ import annotations

import hashlib
import os


class BlobStore:
    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)

    def _confined(self, rel: str) -> str:
        path = os.path.realpath(os.path.join(self.root, rel))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise ValueError("path escapes blob-store root")
        return path

    def put(self, data: bytes) -> tuple[str, str]:
        """Store bytes content-addressed. Returns ``(relative_path, digest)``."""
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        hexpart = digest.split(":", 1)[1]
        rel = os.path.join("blobs", hexpart[:2], hexpart)
        final = self._confined(rel)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        if not os.path.isfile(final):
            tmp = final + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, final)
        return rel, digest

    def get(self, rel: str) -> bytes:
        with open(self._confined(rel), "rb") as fh:
            return fh.read()

    def exists(self, rel: str) -> bool:
        return os.path.isfile(self._confined(rel))

    def delete(self, rel: str) -> bool:
        path = self._confined(rel)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False
