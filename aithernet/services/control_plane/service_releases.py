"""Release, download and support domains (Stage 14F §22-§25, §38).

Releases are never published automatically from a commit — publishing is an explicit release
authority action, and a release cannot be published unsigned. Downloads serve only *published*,
non-revoked artifacts, by digest, with no path traversal (the blob store rejects unsafe keys).
Support cases are tenant-isolated; bundle uploads are explicit and size-bounded.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select

from services.control_plane import release_signing
from services.control_plane.errors import ControlPlaneError, ForbiddenError, NotFoundError
from services.control_plane.models import (
    HostedRelease,
    HostedReleaseArtifact,
    HostedReleaseChannel,
    HostedSupportBundle,
    HostedSupportCase,
    HostedSupportMessage,
)
from services.control_plane.roles import (
    RELEASES_PUBLISH,
    SUPPORT_CREATE,
    SUPPORT_MANAGE,
)

#: Maximum support bundle size (defensive bound, §38).
MAX_SUPPORT_BUNDLE_BYTES = 8 * 1024 * 1024
#: Allowed support-bundle content kinds (defensive content-type check, §38).
_ALLOWED_BUNDLE_MAGIC = (b"\x1f\x8b", b"PK\x03\x04", b"{")  # gzip / zip / json

#: Process-local cache of assembled delivery ZIPs, keyed by ``"{release_id}:{manifest_digest}"``.
#: Release artifacts + manifest are immutable once published, so the digest fully keys the bytes.
_BUNDLE_CACHE: dict[str, dict] = {}


class ReleasesMixin:
    # -- releases ----------------------------------------------------------------------------

    def create_release(
        self, principal, *, version: str, channel: str | None = None,
        minimum_supported_version: str | None = None, os_support: list[str] | None = None,
        architecture: list[str] | None = None, notes: str | None = None,
    ) -> dict:
        self.require_platform_permission(principal, RELEASES_PUBLISH)
        channel = channel or self.config.releases.channel
        import hashlib

        notes_digest = (
            "sha256:" + hashlib.sha256(notes.encode("utf-8")).hexdigest() if notes else None
        )
        with self._session() as s:
            dup = s.execute(
                select(HostedRelease).where(
                    HostedRelease.version == version, HostedRelease.channel == channel
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise ControlPlaneError("release_exists")
            rel = HostedRelease(
                version=version, channel=channel, status="draft",
                minimum_supported_version=minimum_supported_version,
                os_support_json=os_support or [], architecture_json=architecture or [],
                notes_digest=notes_digest, created_at=self.now(),
            )
            s.add(rel)
            self._audit(s, action="release.create", actor_user_id=principal.user_id,
                        target=f"{version}:{channel}")
            s.flush()
            rid = rel.id
            s.commit()
            return {"release_id": rid, "version": version, "channel": channel, "status": "draft"}

    def add_release_artifact(
        self, principal, *, release_id: str, name: str, data: bytes, kind: str | None = None,
        architecture: str = "any", os_family: str = "any",
        content_type: str = "application/octet-stream",
        expected_sha256: str | None = None, expected_byte_size: int | None = None,
    ) -> dict:
        """Store one release artifact, integrity-checked and atomically.

        The filename is validated (no path traversal); the payload is bounded by
        ``releases.max_artifact_bytes``; and, when the uploader declares them, the SHA-256 digest
        and byte count are verified BEFORE anything is written — a mismatch (or an interrupted
        upload) leaves no blob and no row, so a partial/corrupt artifact can never be published.
        """
        from services.control_plane.blobstore import sha256_hex

        self.require_platform_permission(principal, RELEASES_PUBLISH)
        safe_name = _validate_artifact_name(name)
        # Infer the kind from the filename unless the caller passed an explicit one; never silently
        # label everything "wheel".
        resolved_kind = kind if kind else release_signing.infer_kind(safe_name)
        max_bytes = self.config.releases.max_artifact_bytes
        if len(data) > max_bytes:
            raise ControlPlaneError("artifact_too_large")
        digest = sha256_hex(data)
        if expected_sha256 and _norm_digest(expected_sha256) != digest:
            raise ControlPlaneError("artifact_digest_mismatch")
        if expected_byte_size is not None and int(expected_byte_size) != len(data):
            raise ControlPlaneError("artifact_size_mismatch")
        with self._session() as s:
            rel = s.get(HostedRelease, release_id)
            if rel is None:
                raise NotFoundError("release_not_found")
            if rel.status not in ("draft", "candidate"):
                raise ControlPlaneError("release_not_editable")
            object_path = f"releases/{rel.channel}/{rel.version}/{safe_name}"
            sha = self.blob_store.put(object_path, data)
            artifact = HostedReleaseArtifact(
                release_id=release_id, name=safe_name, kind=resolved_kind,
                architecture=architecture,
                os_family=os_family, sha256=sha, byte_size=len(data), content_type=content_type,
                relative_object_path=object_path, created_at=self.now(),
            )
            s.add(artifact)
            # Adding an artifact invalidates any prior signature.
            rel.manifest_signature = None
            rel.manifest_digest = None
            rel.status = "draft"
            self._audit(s, action="release.artifact.add", actor_user_id=principal.user_id,
                        target=name)
            s.commit()
            return {"release_id": release_id, "name": name, "sha256": sha, "byte_size": len(data)}

    def sign_release(self, principal, *, release_id: str, signing_seed_b64: str | None = None,
                     signing_key_id: str | None = None) -> dict:
        """Build the canonical manifest and attach a detached Ed25519 signature (candidate)."""
        self.require_platform_permission(principal, RELEASES_PUBLISH)
        # Resolve + validate the (non-secret) signing-key id against the configured registry. An
        # unknown or revoked id fails closed. The PRIVATE seed is supplied explicitly by the release
        # manager or read from the env var named by the key's ``ref`` — never stored in metadata.
        key_id = signing_key_id or self.config.releases.signing_key_id
        # When no explicit registry is configured, the single configured key id is the active key
        # (``from_env`` builds this registry; direct construction defaults it here). A key id absent
        # from the registry, or one marked revoked, fails closed.
        registry = self.config.releases.signing_keys or {
            self.config.releases.signing_key_id: {
                "ref": self.config.releases.signing_key_ref, "status": "active",
            }
        }
        entry = registry.get(key_id)
        if entry is None:
            raise ControlPlaneError("release_signing_key_unknown")
        if str(entry.get("status", "active")) != "active":
            raise ControlPlaneError("release_signing_key_revoked")
        seed = signing_seed_b64 or _resolve_seed(entry.get("ref")) \
            or self.config.release_signing_key_b64
        if not seed:
            raise ControlPlaneError("release_signing_key_missing")
        with self._session() as s:
            rel = s.get(HostedRelease, release_id)
            if rel is None:
                raise NotFoundError("release_not_found")
            artifacts = s.execute(
                select(HostedReleaseArtifact).where(
                    HostedReleaseArtifact.release_id == release_id
                )
            ).scalars().all()
            if not artifacts:
                raise ControlPlaneError("release_has_no_artifacts")
            manifest = release_signing.canonical_manifest(
                version=rel.version, channel=rel.channel, signing_key_id=key_id,
                artifacts=[_artifact_manifest(a) for a in artifacts],
                minimum_supported_version=rel.minimum_supported_version,
                notes_digest=rel.notes_digest, os_support=rel.os_support_json,
                architecture=rel.architecture_json,
            )
            rel.manifest_digest = release_signing.manifest_digest(manifest)
            rel.manifest_signature = release_signing.sign_manifest(seed, manifest)
            rel.signing_key_id = key_id
            rel.status = "candidate"
            self._audit(s, action="release.sign", actor_user_id=principal.user_id,
                        target=rel.version)
            s.commit()
            return {"release_id": release_id, "manifest_digest": rel.manifest_digest,
                    "signing_key_id": key_id, "status": "candidate"}

    def publish_release(self, principal, *, release_id: str) -> dict:
        self.require_platform_permission(principal, RELEASES_PUBLISH)
        with self._session() as s:
            rel = s.get(HostedRelease, release_id)
            if rel is None:
                raise NotFoundError("release_not_found")
            if not rel.manifest_signature or not rel.manifest_digest:
                raise ControlPlaneError("release_not_signed")
            # Integrity gate: every declared artifact's blob must still exist and match its
            # recorded digest before the release becomes downloadable. A missing or altered blob
            # fails closed — a partial/corrupted release is never published.
            artifacts = s.execute(
                select(HostedReleaseArtifact).where(
                    HostedReleaseArtifact.release_id == release_id
                )
            ).scalars().all()
            if not artifacts:
                raise ControlPlaneError("release_has_no_artifacts")
            from services.control_plane.blobstore import BlobStoreError, sha256_hex
            for a in artifacts:
                try:
                    blob = self.blob_store.get(a.relative_object_path)
                except BlobStoreError as exc:
                    raise ControlPlaneError("release_artifact_missing") from exc
                if sha256_hex(blob) != a.sha256 or len(blob) != a.byte_size:
                    raise ControlPlaneError("release_artifact_corrupt")
            rel.status = "published"
            rel.published_at = self.now()
            channel = s.get(HostedReleaseChannel, rel.channel)
            if channel is None:
                channel = HostedReleaseChannel(name=rel.channel, description=rel.channel)
                s.add(channel)
            channel.current_release_id = rel.id
            self._audit(s, action="release.publish", actor_user_id=principal.user_id,
                        target=f"{rel.version}:{rel.channel}")
            s.commit()
            return {"release_id": release_id, "status": "published",
                    "version": rel.version, "channel": rel.channel}

    def revoke_release(self, principal, *, release_id: str) -> dict:
        self.require_platform_permission(principal, RELEASES_PUBLISH)
        with self._session() as s:
            rel = s.get(HostedRelease, release_id)
            if rel is None:
                raise NotFoundError("release_not_found")
            rel.status = "revoked"
            channel = s.get(HostedReleaseChannel, rel.channel)
            if channel is not None and channel.current_release_id == rel.id:
                channel.current_release_id = None
            self._audit(s, action="release.revoke", actor_user_id=principal.user_id,
                        target=rel.version)
            s.commit()
            return {"release_id": release_id, "status": "revoked"}

    def list_releases(self, principal=None, channel: str | None = None,
                      include_unpublished: bool = False) -> list[dict]:
        with self._session() as s:
            stmt = select(HostedRelease)
            if channel:
                stmt = stmt.where(HostedRelease.channel == channel)
            privileged = include_unpublished and principal is not None and (
                principal.is_platform_admin or self._has_release_authority(principal)
            )
            if not privileged:
                stmt = stmt.where(HostedRelease.status == "published")
            rows = s.execute(stmt.order_by(HostedRelease.created_at.desc())).scalars().all()
            return [self._release_dict(s, r) for r in rows]

    def channel_release(self, channel: str | None = None) -> dict | None:
        channel = channel or self.config.releases.channel
        with self._session() as s:
            ch = s.get(HostedReleaseChannel, channel)
            if ch is None or ch.current_release_id is None:
                return None
            rel = s.get(HostedRelease, ch.current_release_id)
            if rel is None or rel.status != "published":
                return None
            return self._release_dict(s, rel, include_manifest=True)

    def release_manifest(self, release_id: str) -> dict:
        with self._session() as s:
            rel = s.get(HostedRelease, release_id)
            if rel is None or rel.status not in ("published", "candidate"):
                raise NotFoundError("release_not_found")
            return self._release_dict(s, rel, include_manifest=True)

    # -- downloads ---------------------------------------------------------------------------

    def download_artifact(self, *, release_id: str, name: str) -> tuple[bytes, str, str]:
        """Return ``(data, content_type, sha256)`` for a PUBLISHED artifact. Revoked/unpublished
        releases are not downloadable; the blob store rejects any path traversal."""
        with self._session() as s:
            rel = s.get(HostedRelease, release_id)
            if rel is None:
                raise NotFoundError("release_not_found")
            if rel.status != "published":
                raise ForbiddenError("release_not_downloadable")
            artifact = s.execute(
                select(HostedReleaseArtifact).where(
                    HostedReleaseArtifact.release_id == release_id,
                    HostedReleaseArtifact.name == name,
                )
            ).scalar_one_or_none()
            if artifact is None:
                raise NotFoundError("artifact_not_found")
            data = self.blob_store.get(artifact.relative_object_path)
            return data, artifact.content_type, artifact.sha256

    # -- authorized downloads (customer browser / enrolled node / public verification) ---------

    #: Verification files synthesized on demand for a published, signed release (never stored
    #: blobs): the canonical manifest, its detached signature, a PEM public key, and SHA256SUMS.
    #: They are PUBLIC so a customer can fetch and verify a release before authenticating a package
    #: download, and they always correspond exactly to the signed manifest.
    VERIFICATION_FILES = ("manifest.json", "manifest.sig", "SHA256SUMS", "aithernet-release.pub")

    @classmethod
    def _artifact_is_public(cls, name: str) -> bool:
        """The verification set (manifest/sig/SHA256SUMS/PEM key) is downloadable without auth;
        package artifacts are not."""
        return name.endswith(".pub") or name in cls.VERIFICATION_FILES

    def _build_manifest(self, s, rel):
        artifacts = s.execute(
            select(HostedReleaseArtifact).where(HostedReleaseArtifact.release_id == rel.id)
        ).scalars().all()
        manifest = release_signing.canonical_manifest(
            version=rel.version, channel=rel.channel, signing_key_id=rel.signing_key_id,
            artifacts=[_artifact_manifest(a) for a in artifacts],
            minimum_supported_version=rel.minimum_supported_version,
            notes_digest=rel.notes_digest, os_support=rel.os_support_json,
            architecture=rel.architecture_json)
        return manifest, artifacts

    def _verification_payload(self, s, rel, name: str):
        """Synthesize a verification file for a published, signed release, or ``None``.

        ``manifest.json`` is the EXACT canonical bytes the signature covers; ``manifest.sig`` is the
        raw detached Ed25519 signature; ``aithernet-release.pub`` is the production public key as
        PEM; ``SHA256SUMS`` lists every artifact digest plus the manifest. So the served signature
        corresponds exactly to the served manifest and the served key to the manifest's key id.
        """
        if name not in self.VERIFICATION_FILES or not rel.manifest_signature:
            return None
        import base64

        from services.control_plane.blobstore import sha256_hex
        manifest, artifacts = self._build_manifest(s, rel)
        mbytes = release_signing.manifest_bytes(manifest)
        if name == "manifest.json":
            return mbytes, "application/json", sha256_hex(mbytes)
        if name == "manifest.sig":
            raw = base64.b64decode(rel.manifest_signature)
            return raw, "application/octet-stream", sha256_hex(raw)
        if name == "aithernet-release.pub":
            pub_b64 = self.config.release_public_key_b64
            if not pub_b64:
                return None
            pem = release_signing.public_key_pem(pub_b64).encode()
            return pem, "application/x-pem-file", sha256_hex(pem)
        if name == "SHA256SUMS":
            lines = [f"{a.sha256.split(':', 1)[-1]}  {a.name}" for a in artifacts]
            lines.append(f"{sha256_hex(mbytes).split(':', 1)[-1]}  manifest.json")
            content = ("\n".join(sorted(lines)) + "\n").encode()
            return content, "text/plain", sha256_hex(content)
        return None

    def _published_release(self, s, release_id: str):
        rel = s.get(HostedRelease, release_id)
        if rel is None:
            raise NotFoundError("release_not_found")
        if rel.status != "published":  # revoked / draft / candidate are not downloadable
            raise ForbiddenError("release_not_downloadable")
        return rel

    def _fetch_published_artifact(self, s, release_id: str, name: str):
        rel = s.get(HostedRelease, release_id)
        if rel is None:
            raise NotFoundError("release_not_found")
        if rel.status != "published":  # revoked / draft / candidate are not downloadable
            raise ForbiddenError("release_not_downloadable")
        artifact = s.execute(
            select(HostedReleaseArtifact).where(
                HostedReleaseArtifact.release_id == release_id,
                HostedReleaseArtifact.name == name,
            )
        ).scalar_one_or_none()
        if artifact is None:
            raise NotFoundError("artifact_not_found")
        return rel, artifact

    def download_artifact_public(self, *, release_id: str, name: str) -> tuple[bytes, str, str]:
        """PUBLIC: serve ONLY the verification key of a published release; private package
        artifacts require authentication (no more hidden unauthenticated package URLs)."""
        with self._session() as s:
            rel = self._published_release(s, release_id)
            vf = self._verification_payload(s, rel, name)
            if vf is not None:
                return vf
            _rel, artifact = self._fetch_published_artifact(s, release_id, name)
            if not self._artifact_is_public(name):
                raise ForbiddenError("artifact_requires_authentication")
            return (self.blob_store.get(artifact.relative_object_path),
                    artifact.content_type, artifact.sha256)

    def download_artifact_customer(self, principal, *, release_id: str,
                                   name: str) -> tuple[bytes, str, str]:
        """Customer browser: authenticated session + tenant membership + accepted required
        policies + published, non-revoked release. Platform admins (who manage releases) may
        always download."""
        is_admin = bool(getattr(principal, "is_platform_admin", False))
        if not is_admin and not getattr(principal, "memberships", None):
            raise ForbiddenError("not_a_customer")
        with self._session() as s:
            rel = self._published_release(s, release_id)
            if not is_admin and not self._has_accepted_required_policies(s, principal.user_id):
                raise ForbiddenError("required_policies_not_accepted")
            vf = self._verification_payload(s, rel, name)
            if vf is not None:
                return vf
            _rel, artifact = self._fetch_published_artifact(s, release_id, name)
            return (self.blob_store.get(artifact.relative_object_path),
                    artifact.content_type, artifact.sha256)

    def download_artifact_node(self, *, release_id: str, name: str) -> tuple[bytes, str, str]:
        """Enrolled node update download. The route applies node authentication BEFORE calling
        this; here we re-check the release is published + non-revoked."""
        with self._session() as s:
            rel = self._published_release(s, release_id)
            vf = self._verification_payload(s, rel, name)
            if vf is not None:
                return vf
            _rel, artifact = self._fetch_published_artifact(s, release_id, name)
            return (self.blob_store.get(artifact.relative_object_path),
                    artifact.content_type, artifact.sha256)

    @staticmethod
    def _safe_arcname(name: str) -> str:
        """ZIP-traversal-safe entry name: a bare basename with no separators or dot-segments."""
        base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if not base or base in (".", "..") or "/" in base or "\\" in base:
            raise ControlPlaneError("unsafe_artifact_name")
        return base

    def _assemble_release_bundle(self, s, rel) -> dict:
        """Build the Ubuntu delivery ZIP server-side from the EXACT stored beta bytes.

        The ZIP includes EVERY release artifact plus the synthesized verification set
        (manifest.json/manifest.sig/SHA256SUMS/aithernet-release.pub). Including every artifact is
        deliberate: the bundled ``SHA256SUMS`` references every artifact + the manifest, so the
        documented customer command ``sha256sum -c SHA256SUMS`` succeeds from the extracted
        directory with no special logic (defect: the wheel/sdist were referenced but excluded).
        Every real artifact is read from the blob store and re-hashed against its stored digest
        BEFORE inclusion (fail-closed on mismatch). The ZIP is assembled deterministically (sorted
        entries, ZIP_STORED, fixed timestamps) so its SHA-256 is reproducible. The signed manifest +
        contained digests remain the trust root — the ZIP is only a delivery container. Cached by
        the immutable manifest digest so repeated requests don't re-read the blobs.
        """
        import io
        import zipfile

        from services.control_plane.blobstore import sha256_hex

        cache_key = f"{rel.id}:{rel.manifest_digest or ''}"
        cached = _BUNDLE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        entries: list[tuple[str, bytes, str]] = []  # (arcname, bytes, "sha256:..")
        artifacts = s.execute(
            select(HostedReleaseArtifact).where(HostedReleaseArtifact.release_id == rel.id)
        ).scalars().all()
        for a in artifacts:
            data = self.blob_store.get(a.relative_object_path)
            actual = sha256_hex(data)
            if actual != a.sha256:  # fail closed: never serve bytes that drifted from the manifest
                raise ControlPlaneError("artifact_digest_mismatch")
            entries.append((self._safe_arcname(a.name), data, actual))
        for name in self.VERIFICATION_FILES:
            vf = self._verification_payload(s, rel, name)
            if vf is None:
                continue
            data, _ct, sha = vf
            entries.append((self._safe_arcname(name), data, sha))

        entries.sort(key=lambda e: e[0])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
            for arcname, data, _sha in entries:
                info = zipfile.ZipInfo(filename=arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o644 << 16
                zf.writestr(info, data)
        zip_bytes = buf.getvalue()
        result = {
            "filename": f"aithernet-{rel.version}-ubuntu24.04-amd64.zip",
            "zip": zip_bytes,
            "sha256": sha256_hex(zip_bytes),
            "byte_size": len(zip_bytes),
            "files": [{"name": n, "sha256": shv} for n, _d, shv in entries],
        }
        _BUNDLE_CACHE[cache_key] = result
        return result

    def download_release_bundle_customer(self, principal, *, release_id: str) -> dict:
        """Customer browser: the same authorization as a package download (authenticated session +
        tenant membership + accepted required policies + published release), returning the assembled
        Ubuntu delivery ZIP (bytes + filename + SHA-256 + contained file digests)."""
        is_admin = bool(getattr(principal, "is_platform_admin", False))
        if not is_admin and not getattr(principal, "memberships", None):
            raise ForbiddenError("not_a_customer")
        with self._session() as s:
            rel = self._published_release(s, release_id)
            if not is_admin and not self._has_accepted_required_policies(s, principal.user_id):
                raise ForbiddenError("required_policies_not_accepted")
            return self._assemble_release_bundle(s, rel)

    def _has_accepted_required_policies(self, s, user_id: str) -> bool:
        from services.control_plane.models import HostedPolicyAcceptance
        required = self._active_required_policies(s)
        if not required:
            return True
        accepted = {
            (a.policy_type, a.version)
            for a in s.execute(
                select(HostedPolicyAcceptance).where(HostedPolicyAcceptance.user_id == user_id)
            ).scalars()
        }
        return all((d.policy_type, d.version) in accepted for d in required)

    def public_releases(self) -> dict:
        """PUBLIC release metadata + verification material (NO private package bytes). Used by the
        public website / unauthenticated callers to show release notes and verification info."""
        with self._session() as s:
            rows = s.execute(
                select(HostedRelease).where(HostedRelease.status == "published")
                .order_by(HostedRelease.created_at.desc())
            ).scalars().all()
            releases = []
            for rel in rows:
                d = self._release_dict(s, rel)
                for a in d["artifacts"]:
                    a["public"] = self._artifact_is_public(a["name"])
                d["os_support"] = list(rel.os_support_json or [])
                d["architectures"] = list(rel.architecture_json or [])
                releases.append(d)
        return {"releases": releases,
                "verification_public_key": self.config.release_public_key_b64}

    # -- support -----------------------------------------------------------------------------

    def create_support_case(self, principal, *, tenant_id: str, subject: str,
                            category: str = "general") -> dict:
        self.require_tenant_permission(principal, tenant_id, SUPPORT_CREATE)
        with self._session() as s:
            case = HostedSupportCase(
                tenant_id=tenant_id, opener_user_id=principal.user_id, subject=subject[:200],
                category=category[:40], created_at=self.now(),
            )
            s.add(case)
            self._audit(s, action="support.case.create", actor_user_id=principal.user_id,
                        tenant_id=tenant_id, target=subject[:60])
            s.flush()
            cid = case.id
            s.commit()
            return {"case_id": cid, "tenant_id": tenant_id, "subject": subject, "status": "open"}

    def add_support_message(self, principal, *, case_id: str, body: str) -> dict:
        with self._session() as s:
            case = s.get(HostedSupportCase, case_id)
            if case is None:
                raise NotFoundError("case_not_found")
            role = self._support_role(principal, case)
            s.add(HostedSupportMessage(
                case_id=case_id, author_user_id=principal.user_id, author_role=role,
                body=body[:8000], created_at=self.now(),
            ))
            s.commit()
            return {"case_id": case_id, "added": True}

    def upload_support_bundle(self, principal, *, case_id: str, data: bytes) -> dict:
        if len(data) > MAX_SUPPORT_BUNDLE_BYTES:
            raise ControlPlaneError("support_bundle_too_large")
        if not data[:1] or not any(data.startswith(m) for m in _ALLOWED_BUNDLE_MAGIC):
            raise ControlPlaneError("support_bundle_content_rejected")
        with self._session() as s:
            case = s.get(HostedSupportCase, case_id)
            if case is None:
                raise NotFoundError("case_not_found")
            self._support_role(principal, case)
            object_path = f"support/{case.tenant_id}/{case_id}/{self.now().timestamp()}.bundle"
            sha = self.blob_store.put(object_path, data)
            s.add(HostedSupportBundle(
                case_id=case_id, tenant_id=case.tenant_id, uploader_user_id=principal.user_id,
                sha256=sha, byte_size=len(data), relative_object_path=object_path,
                created_at=self.now(),
            ))
            self._audit(s, action="support.bundle.upload", actor_user_id=principal.user_id,
                        tenant_id=case.tenant_id, target=case_id)
            s.commit()
            return {"case_id": case_id, "sha256": sha, "byte_size": len(data)}

    def list_support_cases(self, principal, tenant_id: str | None = None) -> list[dict]:
        with self._session() as s:
            stmt = select(HostedSupportCase)
            manages = principal.is_platform_admin or SUPPORT_MANAGE in _platform_perms(principal)
            if manages and tenant_id is None:
                pass
            elif tenant_id is not None:
                if not manages:
                    self.require_tenant_permission(principal, tenant_id, SUPPORT_CREATE)
                stmt = stmt.where(HostedSupportCase.tenant_id == tenant_id)
            else:
                stmt = stmt.where(HostedSupportCase.tenant_id.in_(principal.tenant_ids() or [""]))
            rows = s.execute(stmt.order_by(HostedSupportCase.created_at.desc())).scalars().all()
            return [_case_dict(c) for c in rows]

    def get_support_case(self, principal, case_id: str) -> dict:
        """Case detail + message thread. Tenant-isolated (the owner's tenant) or support manager."""
        with self._session() as s:
            case = s.get(HostedSupportCase, case_id)
            if case is None:
                raise NotFoundError("case_not_found")
            self._support_role(principal, case)  # authorizes (raises ForbiddenError otherwise)
            messages = s.execute(
                select(HostedSupportMessage).where(
                    HostedSupportMessage.case_id == case_id
                ).order_by(HostedSupportMessage.created_at)
            ).scalars().all()
            return {
                **_case_dict(case),
                "messages": [
                    {"author_role": m.author_role, "body": m.body,
                     "created_at": m.created_at.isoformat()}
                    for m in messages
                ],
            }

    def resolve_support_case(self, principal, *, case_id: str, status: str = "resolved") -> dict:
        self.require_platform_permission(principal, SUPPORT_MANAGE)
        with self._session() as s:
            case = s.get(HostedSupportCase, case_id)
            if case is None:
                raise NotFoundError("case_not_found")
            case.status = status[:16]
            case.assignee_user_id = principal.user_id
            self._audit(s, action="support.case.resolve", actor_user_id=principal.user_id,
                        tenant_id=case.tenant_id, target=case_id)
            s.commit()
            return {"case_id": case_id, "status": case.status}

    # -- internal ----------------------------------------------------------------------------

    def _support_role(self, principal, case: HostedSupportCase) -> str:
        if principal.is_platform_admin or SUPPORT_MANAGE in _platform_perms(principal):
            return "support"
        if case.tenant_id in principal.memberships:
            return "customer"
        raise ForbiddenError("support_case_forbidden")

    def _has_release_authority(self, principal) -> bool:
        return RELEASES_PUBLISH in _platform_perms(principal)

    def _release_dict(self, s, rel: HostedRelease, include_manifest: bool = False) -> dict:
        artifacts = s.execute(
            select(HostedReleaseArtifact).where(HostedReleaseArtifact.release_id == rel.id)
        ).scalars().all()
        out = {
            "release_id": rel.id, "version": rel.version, "channel": rel.channel,
            "status": rel.status, "manifest_digest": rel.manifest_digest,
            "signing_key_id": rel.signing_key_id, "published_at":
                rel.published_at.isoformat() if rel.published_at else None,
            "artifacts": [
                {"name": a.name, "kind": a.kind, "architecture": a.architecture,
                 "os_family": a.os_family, "sha256": a.sha256, "byte_size": a.byte_size,
                 "content_type": a.content_type}
                for a in artifacts
            ],
        }
        if include_manifest and rel.manifest_signature:
            out["manifest_signature"] = rel.manifest_signature
            out["manifest"] = release_signing.canonical_manifest(
                version=rel.version, channel=rel.channel,
                signing_key_id=rel.signing_key_id or self.config.releases.signing_key_id,
                artifacts=[_artifact_manifest(a) for a in artifacts],
                minimum_supported_version=rel.minimum_supported_version,
                notes_digest=rel.notes_digest, os_support=rel.os_support_json,
                architecture=rel.architecture_json,
            )
        return out


#: A release artifact filename is a single path segment of a conservative charset (covers wheel /
#: sdist / ``.deb`` / manifest / checksum / signature / pubkey names, including the ``~`` in Debian
#: versions). No directory separators, ``..``, spaces, or control characters are permitted.
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9._+~-]{1,200}$")


def _validate_artifact_name(name: str) -> str:
    candidate = (name or "").strip()
    if (
        candidate in (".", "..")
        or candidate != Path(candidate).name
        or not _ARTIFACT_NAME_RE.match(candidate)
    ):
        raise ControlPlaneError("invalid_artifact_name")
    return candidate


def _norm_digest(value: str) -> str:
    """Normalize a caller-supplied digest to the stored ``sha256:<hex>`` form (lowercase)."""
    v = (value or "").strip().lower()
    return v if v.startswith("sha256:") else f"sha256:{v}"


def _resolve_seed(ref: str | None) -> str | None:
    """Resolve a signing seed from the env var named by a registry entry's ``ref`` (not stored)."""
    import os
    return os.environ.get(ref) if ref else None


def _platform_perms(principal) -> set[str]:
    from services.control_plane.roles import role_permissions

    perms: set[str] = set()
    for role in principal.platform_roles:
        perms |= role_permissions(role)
    return perms


def _artifact_manifest(a: HostedReleaseArtifact) -> dict:
    return {"name": a.name, "kind": a.kind, "architecture": a.architecture,
            "os_family": a.os_family, "sha256": a.sha256, "byte_size": a.byte_size}


def _case_dict(c: HostedSupportCase) -> dict:
    return {"case_id": c.id, "tenant_id": c.tenant_id, "subject": c.subject,
            "category": c.category, "status": c.status, "opener_user_id": c.opener_user_id,
            "created_at": c.created_at.isoformat()}
