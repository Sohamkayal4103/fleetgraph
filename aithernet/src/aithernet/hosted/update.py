"""Node update client (Stage 14F §24).

Checks the signed release channel, verifies the detached manifest signature against a distributed
release public key, verifies each artifact digest, stages the download and records durable update
history. Rollback restores the previously-applied version. Updates are never unattended by default
and never applied from an unauthenticated URL or a revoked release. Heavy apply/rollback of the
running node reuses the Stage 14A upgrade/rollback mechanisms; this client owns discovery,
verification and staging.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from services.control_plane import release_signing

from aithernet.hosted.client import HostedClient
from aithernet.transport.identity import NodeIdentity


@dataclass
class UpdateDecision:
    available: bool
    current_version: str
    candidate_version: str | None
    is_newer: bool
    reason: str


class UpdateError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UpdateClient:
    def __init__(self, client: HostedClient, *, release_public_key_b64: str,
                 staging_dir: str | Path) -> None:
        self.client = client
        self.release_public_key = release_public_key_b64
        self.staging_dir = Path(staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def check(self, *, identity: NodeIdentity, tenant_id: str, node_id: str,
              current_version: str) -> UpdateDecision:
        rel = self.client.get_release(identity=identity, tenant_id=tenant_id, node_id=node_id)
        if not rel or rel.get("available") is False or not rel.get("version"):
            return UpdateDecision(False, current_version, None, False, "no_release")
        candidate = rel["version"]
        newer = _version_tuple(candidate) > _version_tuple(current_version)
        return UpdateDecision(True, current_version, candidate, newer,
                              "newer" if newer else "current")

    def download_and_verify(self, *, identity: NodeIdentity, tenant_id: str, node_id: str) -> dict:
        """Verify the signed manifest, download every artifact, verify digests. Returns a staged
        plan. Raises :class:`UpdateError` on any signature/digest/revocation failure."""
        rel = self.client.get_release(identity=identity, tenant_id=tenant_id, node_id=node_id)
        if not rel or not rel.get("manifest") or not rel.get("manifest_signature"):
            raise UpdateError("release_unsigned")
        manifest = rel["manifest"]
        if not release_signing.verify_manifest(
                self.release_public_key, manifest, rel["manifest_signature"]):
            raise UpdateError("manifest_signature_invalid")
        staged: list[dict] = []
        for artifact in manifest["artifacts"]:
            # Authenticated (signed) node download — no longer a public artifact URL.
            data, sha = self.client.download(
                rel["release_id"], artifact["name"],
                identity=identity, tenant_id=tenant_id, node_id=node_id)
            if sha != artifact["sha256"] or not release_signing.verify_artifact(
                    data, artifact["sha256"]):
                raise UpdateError("artifact_digest_mismatch")
            dest = self.staging_dir / artifact["name"]
            dest.write_bytes(data)
            staged.append({"name": artifact["name"], "path": str(dest), "sha256": sha})
        plan = {"version": rel["version"], "release_id": rel["release_id"], "artifacts": staged,
                "verified": True}
        (self.staging_dir / "update-plan.json").write_text(json.dumps(plan, indent=2))
        self._record(self.staging_dir, "download_verified", plan["version"])
        return plan

    def record_apply(self, version: str, previous_version: str) -> None:
        self._record(self.staging_dir, "applied", version, previous=previous_version)

    def rollback_target(self) -> str | None:
        history = self._history(self.staging_dir)
        applied = [h for h in history if h["event"] == "applied"]
        return applied[-1].get("previous") if applied else None

    def record_rollback(self, to_version: str) -> None:
        self._record(self.staging_dir, "rolled_back", to_version)

    # -- durable history ---------------------------------------------------------------------

    def _record(self, staging: Path, event: str, version: str, previous: str | None = None) -> None:
        history = self._history(staging)
        history.append({"event": event, "version": version, "previous": previous})
        (staging / "update-history.json").write_text(json.dumps(history, indent=2))

    def _history(self, staging: Path) -> list[dict]:
        path = staging / "update-history.json"
        if not path.is_file():
            return []
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []


def _version_tuple(version: str) -> tuple:
    """Compare versions deterministically (release > prerelease).

    Accepts BOTH the dotted form a release manifest carries (``0.8.0-beta.3``) and the
    PEP 440 normalized form ``importlib.metadata`` reports for the installed package
    (``0.8.0b3``). When ``packaging`` is importable we defer to it so the two forms compare
    equal; the manual parser below is the fallback and understands ``aN``/``bN``/``rcN``.
    """
    try:
        from packaging.version import InvalidVersion, Version
        try:
            v = Version(version)
            # release tuple, then prerelease rank: a real release (pre is None) sorts highest.
            pre_rank = (1,) if v.pre is None else (0, {"a": 0, "b": 1, "rc": 2}.get(v.pre[0], 0),
                                                   v.pre[1])
            return (v.release, pre_rank)
        except InvalidVersion:
            pass
    except Exception:  # noqa: BLE001 — packaging missing; fall back to the manual parser
        pass
    core, _, pre = version.partition("-")
    parts = tuple(int(x) if x.isdigit() else 0 for x in core.split("."))
    # A version with no prerelease sorts after the same core with a prerelease.
    pre_rank = (1,) if not pre else (0, *[int(x) if x.isdigit() else 0 for x in
                                          pre.replace(".", " ").split() if x])
    return (parts, pre_rank)
