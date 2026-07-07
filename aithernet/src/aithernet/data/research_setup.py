"""beta.9 — one-shot owner-research enablement shared by `aithernet setup` and `data setup`.

DEFAULT (hosted owner archive): enable ``owner_full`` recording, record explicit consent, select
the ``hosted_owner_archive`` upload mode, initialize the local spool, and (when the node is already
enrolled) mint a Research Upload Capability so completed missions auto-upload SANITIZED records to
the Aithernet HOSTED owner archive. The owner Google Drive archive is managed SERVER-SIDE — the
client never touches Google, never installs ``oauth-client.json``, and never manages Drive sync.

Advanced (``standalone_drive``): the beta.8 behaviour where the node uploads to its OWN Google Drive
(kept for operator/standalone use only; not the normal hosted-client path).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

#: An operator's existing shared research Drive root (advanced standalone mode only).
RESEARCH_DRIVE_ROOT_ID = os.environ.get("AITHERNET_RESEARCH_DRIVE_ROOT_ID") or None


def _update_collection(state_root: Path | None, **fields) -> Path:
    """Merge the given ``data_platform.collection.*`` fields into the canonical node.yaml."""
    import yaml

    from aithernet.agents.providers import node_config_path
    path = node_config_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            data = {}
    dp = data.setdefault("data_platform", {})
    coll = dp.setdefault("collection", {})
    coll.update(fields)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=True))
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def set_collection_mode(mode: str, state_root: Path | None = None) -> Path:
    return _update_collection(state_root, data_collection_mode=mode)


def set_auto_sync(enabled: bool, state_root: Path | None = None) -> Path:
    """[standalone_drive only] toggle opportunistic client Drive sync."""
    return _update_collection(state_root, research_auto_sync=bool(enabled))


def set_upload_mode(mode: str, state_root: Path | None = None) -> Path:
    """``hosted_owner_archive`` (default) | ``standalone_drive`` (advanced)."""
    return _update_collection(state_root, research_upload_mode=mode)


def set_consent(granted: bool, state_root: Path | None = None) -> Path:
    """Record explicit, withdrawable consent to upload to the Aithernet owner archive."""
    return _update_collection(
        state_root, research_consent=bool(granted),
        research_consent_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def enable_owner_research(*, state_root: Path | None = None, standalone_drive: bool = False,
                          skip_drive: bool = False, drive_client=None, oauth=None,
                          expected_root_id: str | None = RESEARCH_DRIVE_ROOT_ID) -> dict:
    """Enable sanitized research recording + owner-archive upload. Returns a plain-language report.

    DEFAULT is the hosted owner-archive path (no Google on the client). ``standalone_drive`` selects
    the advanced local-Drive mode."""
    from aithernet.data.research_spool import ResearchSpool
    report: dict = {"collection_mode": "owner_full", "steps": []}

    cfg_path = set_collection_mode("owner_full", state_root)
    report["config"] = str(cfg_path)
    report["steps"].append("Enabled full internal research recording (owner_full).")

    spool = ResearchSpool()
    layout = spool.ensure_layout()
    report["spool_root"] = layout["root"]
    report["steps"].append(f"Initialized the local research spool ({len(layout['subdirs'])} "
                           "stages, short-lived queue).")
    report["recorder"] = "active"
    report["steps"].append("Automatic mission recording is active — completed missions now write "
                           "sanitized local research records (no manual `data collect` needed).")

    if standalone_drive:
        set_upload_mode("standalone_drive", state_root)
        return _enable_standalone_drive(report, state_root, skip_drive, drive_client, oauth,
                                        expected_root_id)

    # -- default: hosted owner archive ----------------------------------------
    set_upload_mode("hosted_owner_archive", state_root)
    set_consent(True, state_root)
    report["consent"] = "granted"
    report["mode"] = "hosted_owner_archive"

    from aithernet.data.research_upload import ensure_capability, is_enrolled
    if not is_enrolled(state_root):
        report["owner_archive_upload"] = "pending_enrollment"
        report["steps"].append(
            "Research recording enabled locally. Owner archive upload will begin after enrollment "
            "(no Google Drive setup is required on this client).")
        return report

    cap, reason = ensure_capability(state_root, consent=True)
    if cap is None:
        report["owner_archive_upload"] = "pending"
        report["owner_archive_reason"] = reason
        report["steps"].append(
            f"Research recording enabled locally. Owner archive upload is pending: {reason}. "
            "Aithernet will retry automatically after the issue is repaired.")
        return report
    report["owner_archive_upload"] = "enabled"
    report["steps"].append(
        "Owner archive upload enabled using the enrolled Aithernet hosted connection. Google Drive "
        "archive is managed by hosted Aithernet; no Google Drive setup is required on this client.")
    return report


def _enable_standalone_drive(report, state_root, skip_drive, drive_client, oauth,
                             expected_root_id) -> dict:
    """Advanced/standalone: the beta.8 client-Drive path (not the normal hosted-client flow)."""
    report["mode"] = "standalone_drive"
    if skip_drive:
        report["drive"] = "skipped"
        return report
    from aithernet.data.destinations.google_drive_client import oauth_client_present
    if not oauth_client_present():
        set_auto_sync(False, state_root)
        report["drive"] = "unavailable"
        report["drive_reason"] = "oauth-client.json missing"
        report["steps"].append(
            "[standalone] Drive sync could not be completed because no Google OAuth client is "
            "installed. Install one with `aithernet data drive-client install <file>`.")
        return report
    if oauth is None:
        from aithernet.data.destinations.google_drive_client import GoogleDriveOAuth
        oauth = GoogleDriveOAuth()
    if not oauth.authorized():
        set_auto_sync(False, state_root)
        report["drive"] = "not_authorized"
        report["steps"].append("[standalone] Local recording enabled; Drive sync disabled until "
                               "authorized — run `aithernet data verify --authorize` once.")
        return report
    try:
        oauth.access_token()
    except Exception as exc:  # noqa: BLE001
        set_auto_sync(False, state_root)
        report["drive"] = f"refresh_failed:{type(exc).__name__}"
        return report
    if drive_client is None:
        from aithernet.data.destinations.google_drive_client import RealGoogleDriveClient
        drive_client = RealGoogleDriveClient(oauth)
    from aithernet.data.research_drive import ResearchDriveArchive
    result = ResearchDriveArchive(drive_client).setup_and_self_test()
    report["drive_root_id"] = result["root_folder_id"]
    report["drive_folder_count"] = len(result["folders"])
    report["reused_existing_root"] = (expected_root_id is None
                                      or result["root_folder_id"] == expected_root_id)
    report["self_test"] = result["self_test"]
    set_auto_sync(True, state_root)
    report["drive"] = "ready"
    report["steps"].append("[standalone] Verified the dedicated Drive hierarchy and ran a verified "
                           "test upload. Automatic post-mission sync enabled.")
    return report
