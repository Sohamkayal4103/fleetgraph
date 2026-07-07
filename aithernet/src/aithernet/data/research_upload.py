"""beta.9 — client → hosted owner-archive uploader.

The DEFAULT hosted-client research path. When a mission completes and the operator has consented,
sanitized research packages are sealed and uploaded to the **Aithernet hosted** control plane using
the enrolled node's Ed25519 identity + a scoped Research Upload Capability. The owner Google Drive
archive is managed SERVER-SIDE — the client never touches Google, never holds owner credentials,
never manages Drive sync, and never requires ``oauth-client.json``.

Reliability model: a short-lived local queue (research spool snapshots) + automatic background
upload with retry; on a hosted ACK the uploaded snapshot is compacted away (ACK-based cleanup). A
failed upload leaves the package queued and surfaces in ``aithernet data status``.

Nothing here talks to Google Drive. The capability token is stored 0600 and is presented ONLY to
Aithernet hosted ingestion.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


# -- state resolution ---------------------------------------------------------------------------
def _state_root(state_root: Path | None = None) -> Path:
    if state_root is not None:
        return Path(state_root)
    env = os.environ.get("AITHERNET_STATE_ROOT")
    if env:
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "aithernet"


def _config_dir(state_root: Path | None = None) -> Path:
    return _state_root(state_root) / "config"


def _load_enrollment(state_root: Path | None = None):
    try:
        from aithernet.hosted.localconfig import load_enrollment
        return load_enrollment(_config_dir(state_root))
    except Exception:  # noqa: BLE001
        return None


def _load_identity(state_root: Path | None = None, *, node_id: str | None = None):
    try:
        from aithernet.transport.identity import IdentityManager
        mgr = IdentityManager(_state_root(state_root) / "identity",
                              node_id=node_id or "node", node_name=node_id or "node")
        return mgr.load_or_none()
    except Exception:  # noqa: BLE001
        return None


def is_enrolled(state_root: Path | None = None) -> bool:
    enr = _load_enrollment(state_root)
    return bool(enr and getattr(enr, "enrollment_state", "") == "enrolled"
                and getattr(enr, "control_plane_base_url", None))


# -- capability store (0600, never in hosted.json which strips tokens) --------------------------
def capability_path(state_root: Path | None = None) -> Path:
    return _config_dir(state_root) / "research-capability.json"


def load_capability(state_root: Path | None = None) -> dict | None:
    p = capability_path(state_root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_capability(cap: dict, state_root: Path | None = None) -> None:
    d = _config_dir(state_root)
    d.mkdir(parents=True, exist_ok=True)
    p = capability_path(state_root)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cap, fh)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def clear_capability(state_root: Path | None = None) -> None:
    p = capability_path(state_root)
    try:
        p.unlink()
    except OSError:
        pass


def _capability_valid(cap: dict | None) -> bool:
    if not cap or not cap.get("capability_token"):
        return False
    exp = cap.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(exp.replace("Z", "+00:00")) <= datetime.now(UTC):
                return False
        except ValueError:
            pass
    return True


# -- capability minting -------------------------------------------------------------------------
def ensure_capability(state_root: Path | None = None, *, consent: bool = True,
                      force: bool = False) -> tuple[dict | None, str]:
    """Return (capability, reason). Mints one from the hosted control plane when enrolled +
    consented + missing/expired. reason is a machine tag for status (never a secret)."""
    if not consent:
        return None, "no_consent"
    enr = _load_enrollment(state_root)
    if not is_enrolled(state_root) or enr is None:
        return None, "not_enrolled"
    cap = load_capability(state_root)
    if _capability_valid(cap) and not force:
        return cap, "ok"
    ident = _load_identity(state_root)
    if ident is None:
        return None, "no_identity"
    try:
        from aithernet.hosted.client import HostedClient, HostedClientError
        client = HostedClient(enr.control_plane_base_url)
        minted = client.mint_research_capability(
            identity=ident, tenant_id=enr.tenant_id, node_id=ident.node_id, consent=True)
    except HostedClientError as exc:
        return None, f"mint_failed:{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"mint_failed:{type(exc).__name__}"
    save_capability(minted, state_root)
    return minted, "minted"


# -- hosted reachability ------------------------------------------------------------------------
def hosted_ingestion_status(state_root: Path | None = None) -> str:
    if not is_enrolled(state_root):
        return "not_enrolled"
    enr = _load_enrollment(state_root)
    try:
        import httpx
        r = httpx.get(enr.control_plane_base_url.rstrip("/") + "/health/ready", timeout=5.0)
        return "healthy" if r.status_code == 200 else "degraded"
    except Exception:  # noqa: BLE001
        return "unreachable"


# -- package upload -----------------------------------------------------------------------------
def _seal_snapshot(snapshot: dict, *, tenant_id: str, node_id: str, digest: str) -> bytes:
    from aithernet.data.batch import seal_batch
    records = snapshot.get("records") if isinstance(snapshot, dict) else None
    records = records if isinstance(records, list) else [snapshot]
    sealed = seal_batch(
        batch_id=digest[:32], tenant_id=tenant_id, node_pseudonym=node_id,
        destination_id="owner_archive", idempotency_key=digest,
        record_envelopes=records, consent_summary={"research": "granted"},
        retention_summary={"managed_by": "hosted"},
        created_at_iso=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return sealed.bundle


def upload_pending(spool=None, state_root: Path | None = None, *, consent: bool = True,
                   collect_first: bool = True, compact: bool = True) -> dict:
    """Package pending records + upload every not-yet-uploaded snapshot to the hosted owner archive.

    Returns counts + a status. Never raises on a network/auth failure — leaves packages queued."""
    from aithernet.data.research_spool import ResearchSpool
    sp = spool or ResearchSpool()
    if collect_first:
        try:
            from aithernet.data.research_sync import collect
            collect(spool=sp)
        except Exception:  # noqa: BLE001
            pass

    if not is_enrolled(state_root):
        return {"ok": False, "reason": "pending_enrollment", "uploaded": 0, "failed": 0,
                "queued": len(sp.pending_upload()),
                "next_step": "Enroll this node to enable hosted owner-archive upload."}
    cap, reason = ensure_capability(state_root, consent=consent)
    if cap is None:
        return {"ok": False, "reason": reason, "uploaded": 0, "failed": 0,
                "queued": len(sp.pending_upload()),
                "next_step": "Aithernet will retry upload automatically."}

    enr = _load_enrollment(state_root)
    ident = _load_identity(state_root)
    if ident is None:
        return {"ok": False, "reason": "no_identity", "uploaded": 0, "failed": 0,
                "queued": len(sp.pending_upload())}
    from aithernet.hosted.client import HostedClient, HostedClientError
    client = HostedClient(enr.control_plane_base_url)
    token = cap["capability_token"]

    uploaded = failed = 0
    already = sp.uploaded_digests()
    for digest in sp.snapshot_digests():
        if digest in already:
            continue
        data = sp.read_snapshot_bytes(digest)
        if data is None:
            continue
        try:
            snapshot = json.loads(data)
        except json.JSONDecodeError:
            snapshot = {}
        try:
            bundle = _seal_snapshot(snapshot, tenant_id=enr.tenant_id, node_id=ident.node_id,
                                    digest=digest)
            ack = client.upload_research_package(
                identity=ident, tenant_id=enr.tenant_id, node_id=ident.node_id, bundle=bundle,
                capability_token=token, idempotency_key=digest)
        except HostedClientError as exc:
            failed += 1
            sp._append_ledger("upload-failed", {"digest": digest, "error": exc.code})
            if exc.code in ("hosted_error",) and "capability" in str(exc.detail or ""):
                clear_capability(state_root)  # force re-mint next round
            continue
        except Exception as exc:  # noqa: BLE001
            failed += 1
            sp._append_ledger("upload-failed", {"digest": digest, "error": type(exc).__name__})
            continue
        sp.mark_uploaded(digest=digest, file_id=ack.get("package_id"),
                         byte_size=len(bundle))
        uploaded += 1
        if compact:
            try:
                _compact_snapshot(sp, digest)
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True, "uploaded": uploaded, "failed": failed,
            "queued": len(sp.pending_upload())}


def _compact_snapshot(sp, digest: str) -> None:
    """ACK-based cleanup: remove the uploaded snapshot + its raw records + manifest (the ledger and
    the 'uploaded' record persist as the audit trail)."""
    root = sp.root
    manifest_path = root / "manifests" / f"{digest}.json"
    raw_digests: list[str] = []
    if manifest_path.is_file():
        try:
            raw_digests = json.loads(manifest_path.read_text()).get("raw_digests", [])
        except json.JSONDecodeError:
            raw_digests = []
    for h in raw_digests:
        (root / "raw" / f"{h}.json").unlink(missing_ok=True)
    (root / "snapshots" / f"{digest}.json").unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)


# -- status helpers -----------------------------------------------------------------------------
def owner_archive_state(state_root: Path | None = None, *, consent: bool = True,
                        spool=None) -> dict:
    """A secret-free view of the owner-archive upload state for `aithernet data status`."""
    from aithernet.data.research_spool import ResearchSpool
    sp = spool or ResearchSpool()
    queued = (len(sp.pending_for_collection()) + len(sp.pending_upload())
              if sp.root.is_dir() else 0)
    if not consent:
        return {"owner_archive_upload": "disabled", "local_queue": queued,
                "hosted_ingestion": "not_configured", "drive_sync": "managed_by_hosted"}
    if not is_enrolled(state_root):
        return {"owner_archive_upload": "pending_enrollment", "local_queue": queued,
                "hosted_ingestion": "not_enrolled", "drive_sync": "managed_by_hosted",
                "next_step": "Enroll this node to enable hosted owner-archive upload."}
    hosted = hosted_ingestion_status(state_root)
    cap = load_capability(state_root)
    if hosted != "healthy" or queued > 0:
        return {"owner_archive_upload": "queued" if queued else "enabled",
                "local_queue": queued, "hosted_ingestion": hosted,
                "capability": "present" if _capability_valid(cap) else "pending",
                "drive_sync": "managed_by_hosted",
                "next_step": (None if hosted == "healthy"
                              else "Aithernet will retry upload automatically.")}
    return {"owner_archive_upload": "enabled", "local_queue": 0, "hosted_ingestion": hosted,
            "capability": "present" if _capability_valid(cap) else "pending",
            "drive_sync": "managed_by_hosted", "next_step": None}


def last_uploaded_at(spool=None) -> str | None:
    from aithernet.data.research_spool import ResearchSpool
    sp = spool or ResearchSpool()
    for line in reversed(sp._ledger_lines()):
        if line.get("kind") == "uploaded":
            return line.get("at")
    return None


def maybe_upload_owner_archive(runtime, *, spool=None) -> dict | None:
    """Best-effort post-completion hosted upload. Returns None when not applicable. Fully guarded —
    an upload failure NEVER affects the completed mission; failures surface in status."""
    try:
        coll = runtime.config.data_platform.collection
        if getattr(coll, "research_upload_mode", "hosted_owner_archive") != "hosted_owner_archive":
            return None
        if not getattr(coll, "research_consent", False):
            return None
        return upload_pending(spool=spool, consent=True)
    except Exception:  # noqa: BLE001
        return None
