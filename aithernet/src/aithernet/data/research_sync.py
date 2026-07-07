"""beta.8: package (`collect`) and upload (`sync`) research records, plus a shared Drive-state
helper used by `data status`/`verify`/`doctor`.

- ``collect`` folds every not-yet-packaged ``raw/`` record into a content-addressed snapshot
  package + manifest (idempotent; safe to re-run).
- ``sync`` uploads not-yet-uploaded snapshot packages to the verified Google Drive archive,
  maintaining an idempotent upload ledger. It refuses clearly — never with a raw ``DriveError`` —
  when no OAuth client is installed or Drive is not authorized.
- ``drive_state`` returns a single, secret-free view of Drive readiness (unavailable /
  not_authorized / authorized) with the exact next action.

Nothing here uploads secrets: records are secret-scanned when written to the spool and again when a
snapshot package is built; only sanitized snapshots are eligible for upload.
"""

from __future__ import annotations

from datetime import UTC, datetime


def _iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spool(spool=None):
    if spool is not None:
        return spool
    from aithernet.data.research_spool import ResearchSpool
    return ResearchSpool()


# -- Drive readiness (shared, secret-free) ------------------------------------------------------

def drive_state(*, oauth=None) -> dict:
    """One canonical, secret-free Drive-readiness view.

    ``status`` is one of ``unavailable`` (no OAuth client installed), ``not_authorized`` (client
    installed but no refresh token yet), or ``authorized``. Includes the exact next action."""
    from aithernet.data.destinations.google_drive_client import (
        GoogleDriveOAuth,
        oauth_client_present,
        oauth_client_status,
    )
    if not oauth_client_present():
        st = oauth_client_status()
        return {
            "status": "unavailable",
            "reason": st.get("reason", "oauth-client.json missing"),
            "repair": st.get("repair"),
            "next_step": st.get("repair"),
            "authorized": False,
        }
    try:
        authorized = (oauth or GoogleDriveOAuth()).authorized()
    except Exception:  # noqa: BLE001 — never leak a token; treat as not authorized
        authorized = False
    if not authorized:
        return {
            "status": "not_authorized",
            "reason": "Google Drive is not authorized on this node",
            "next_step": "aithernet data verify --authorize",
            "authorized": False,
            "client": oauth_client_status(),
        }
    return {"status": "authorized", "authorized": True, "client": oauth_client_status()}


# -- collect ------------------------------------------------------------------------------------

def collect(*, spool=None) -> dict:
    """Package every not-yet-collected raw record into one snapshot + manifest (idempotent)."""
    sp = _spool(spool)
    sp.ensure_layout()
    pending = sp.pending_for_collection()
    if not pending:
        return {"collected": 0, "package": None, "pending_before": 0,
                "message": "No new records to collect."}
    records = []
    for h in pending:
        rec = sp.read_raw_record(h)
        if rec is not None:
            records.append(rec)
    package = {
        "schema": "aithernet.dataset.snapshot.v1",
        "created_at": _iso(),
        "count": len(records),
        "raw_digests": pending,
        "records": records,
    }
    receipt = sp.write_snapshot_package(package)
    if receipt.get("quarantined"):
        return {"collected": 0, "package": None, "pending_before": len(pending),
                "quarantined": True,
                "message": "A residual secret was detected while packaging; the snapshot was "
                           "quarantined and NOT made available for upload."}
    return {"collected": len(pending), "pending_before": len(pending),
            "package": receipt["digest"], "manifest_path": receipt.get("manifest_path"),
            "snapshots": sp.counts().get("snapshots", 0)}


# -- sync ---------------------------------------------------------------------------------------

def sync(*, spool=None, oauth=None, drive_client=None, collect_first: bool = True) -> dict:
    """Upload not-yet-uploaded snapshot packages to the verified Drive archive.

    Refuses clearly (structured ``ok=False`` + ``reason`` + ``next_step``) when no OAuth client is
    installed or Drive is not authorized — never a raw traceback/DriveError. Idempotent per package
    via the upload ledger. Returns uploaded/skipped/failed counts."""
    sp = _spool(spool)
    state = drive_state(oauth=oauth)
    if state["status"] != "authorized":
        return {"ok": False, "reason": state["status"], "message": state.get("reason"),
                "next_step": state.get("next_step") or state.get("repair"),
                "uploaded": 0, "skipped": 0, "failed": 0}

    if collect_first:
        try:
            collect(spool=sp)
        except Exception:  # noqa: BLE001 — packaging failure must not abort an otherwise-ok sync
            pass

    from aithernet.data.destinations.google_drive_client import (
        GoogleDriveOAuth,
        RealGoogleDriveClient,
    )
    from aithernet.data.research_drive import ResearchDriveArchive
    oauth = oauth or GoogleDriveOAuth()
    client = drive_client or RealGoogleDriveClient(oauth)
    archive = ResearchDriveArchive(client)
    try:
        folders = archive.ensure_hierarchy()
    except Exception as exc:  # noqa: BLE001 — surface a category, never a token
        return {"ok": False, "reason": "drive_error", "message": type(exc).__name__,
                "next_step": "aithernet data verify --authorize", "uploaded": 0,
                "skipped": 0, "failed": 0}
    target = folders.get("60 Dataset Snapshots") or folders.get("_root")

    uploaded = skipped = failed = 0
    already = sp.uploaded_digests()
    for h in sp.snapshot_digests():
        if h in already:
            skipped += 1
            continue
        data = sp.read_snapshot_bytes(h)
        if data is None:
            continue
        try:
            rec = archive.upload_verified(folder_id=target, name=f"{h}.json", data=data,
                                          idempotency_key=h)
            sp.mark_uploaded(digest=h, file_id=rec.get("file_id"), byte_size=rec.get("byte_size"))
            uploaded += 1
        except Exception as exc:  # noqa: BLE001 — record the failure category, keep going
            failed += 1
            sp._append_ledger("upload-failed", {"digest": h, "error_type": type(exc).__name__})
    return {"ok": True, "uploaded": uploaded, "skipped": skipped, "failed": failed,
            "pending_upload": len(sp.pending_upload())}


def maybe_auto_sync(runtime, *, spool=None) -> dict | None:
    """Best-effort post-completion auto-sync. Returns ``None`` when auto-sync is off/unavailable.

    Only runs when ``research_auto_sync`` is configured AND Drive is authorized. Fully guarded — a
    sync failure here must never affect the completed mission; failures surface in status/doctor."""
    try:
        coll = runtime.config.data_platform.collection
        # beta.9: client Drive sync is the ADVANCED standalone/local-archive mode ONLY. The default
        # hosted-client path uploads to the Aithernet owner archive (research_upload), not Drive.
        if getattr(coll, "research_upload_mode", "hosted_owner_archive") != "standalone_drive":
            return None
        if not getattr(coll, "research_auto_sync", False):
            return None
        if drive_state()["status"] != "authorized":
            return None
        return sync(spool=spool)
    except Exception:  # noqa: BLE001
        return None
