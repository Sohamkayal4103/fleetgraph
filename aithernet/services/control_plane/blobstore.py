"""Hosted blob storage (Stage 14F §26).

Stores release artifacts, support bundles and selected portal downloads. ``LocalHostedBlobStore``
writes atomically under a root with NO path traversal. ``S3CompatibleHostedBlobStore`` takes an
*injected* client (a boto3-style ``put_object``/``get_object``/``delete_object`` object), so the
adapter is software-tested with a local fake — no real S3 credentials or bucket are required.
Google Drive is an encrypted *archive* destination, never the live blob backend.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol


class BlobStoreError(RuntimeError):
    pass


def _safe_relpath(relative_path: str) -> str:
    """Reject absolute paths and traversal; return a normalized POSIX-ish relative key."""
    rel = relative_path.strip()
    if not rel or rel.startswith("/") or os.path.isabs(rel) or "\\" in rel:
        raise BlobStoreError("unsafe_object_path")
    if rel != os.path.normpath(rel) or rel.startswith(".."):
        raise BlobStoreError("unsafe_object_path")
    if ".." in Path(rel).parts:
        raise BlobStoreError("unsafe_object_path")
    return rel


class HostedBlobStore(Protocol):
    def put(self, relative_path: str, data: bytes) -> str: ...
    def get(self, relative_path: str) -> bytes: ...
    def delete(self, relative_path: str) -> None: ...
    def exists(self, relative_path: str) -> bool: ...


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class LocalHostedBlobStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, relative_path: str) -> Path:
        rel = _safe_relpath(relative_path)
        full = (self.root / rel).resolve()
        if not str(full).startswith(str(self.root.resolve())):
            raise BlobStoreError("unsafe_object_path")
        return full

    def put(self, relative_path: str, data: bytes) -> str:
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return sha256_hex(data)

    def get(self, relative_path: str) -> bytes:
        path = self._path(relative_path)
        if not path.exists():
            raise BlobStoreError("object_not_found")
        return path.read_bytes()

    def delete(self, relative_path: str) -> None:
        path = self._path(relative_path)
        if path.exists():
            path.unlink()

    def exists(self, relative_path: str) -> bool:
        return self._path(relative_path).exists()


class S3CompatibleHostedBlobStore:
    """Adapter over an injected boto3-style S3 client (tested with a local fake)."""

    def __init__(self, client: object, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def put(self, relative_path: str, data: bytes) -> str:
        key = _safe_relpath(relative_path)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)  # type: ignore[attr-defined]
        return sha256_hex(data)

    def get(self, relative_path: str) -> bytes:
        key = _safe_relpath(relative_path)
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise BlobStoreError("object_not_found") from exc
        body = resp["Body"]
        return body.read() if hasattr(body, "read") else bytes(body)

    def delete(self, relative_path: str) -> None:
        key = _safe_relpath(relative_path)
        self.client.delete_object(Bucket=self.bucket, Key=key)  # type: ignore[attr-defined]

    def exists(self, relative_path: str) -> bool:
        key = _safe_relpath(relative_path)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)  # type: ignore[attr-defined]
            return True
        except Exception:  # noqa: BLE001
            return False


class FakeS3Client:
    """An in-memory boto3-style client for software tests (no real S3)."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict:  # noqa: N803
        self._objects[(Bucket, Key)] = bytes(Body)
        return {"ETag": sha256_hex(bytes(Body))}

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        data = self._objects[(Bucket, Key)]
        return {"Body": _BytesBody(data)}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        self._objects.pop((Bucket, Key), None)
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if (Bucket, Key) not in self._objects:
            raise KeyError("not_found")
        return {"ContentLength": len(self._objects[(Bucket, Key)])}


class _BytesBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def build_blob_store(config) -> HostedBlobStore:
    """Construct the configured blob store. Production S3 needs an injected client elsewhere."""
    from services.control_plane.config import HostedConfig

    assert isinstance(config, HostedConfig)
    if config.object_storage.backend == "s3":
        raise BlobStoreError("s3_requires_injected_client")  # constructed explicitly in deployment
    return LocalHostedBlobStore(config.object_storage.local_root)
