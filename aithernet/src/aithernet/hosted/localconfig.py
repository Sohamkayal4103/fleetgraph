"""Managed local hosted-enrollment configuration (Stage 14F §18).

Written outside the repository under the node state root's ``config/`` directory, atomically and
with restrictive permissions (0600). It stores only the hosted base URL, the hosted node id, the
tenant id, the release channel, the policy bundle version and the enrollment state — NEVER a
plaintext website password, a portal session cookie, an unrelated OAuth token, or the private key
(the identity key stays in the 0700 ``identity/`` store, not in general config).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

HOSTED_CONFIG_FILE = "hosted.json"

#: Keys that must never be written to the managed hosted config (defensive).
_FORBIDDEN_KEYS = frozenset({"password", "session", "cookie", "private_key", "secret", "token"})


@dataclass
class HostedEnrollment:
    control_plane_base_url: str = ""
    ingestion_base_url: str = ""
    hosted_node_id: str = ""
    tenant_id: str = ""
    release_channel: str = "early-access"
    policy_bundle_version: str = ""
    enrollment_state: str = "unenrolled"  # unenrolled | enrolled | revoked
    # The node authenticates with its OWN identity key; this names the key id only (non-secret).
    credential_key_id: str = "default"

    def to_public_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k.lower() not in _FORBIDDEN_KEYS}


def hosted_config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / HOSTED_CONFIG_FILE


def load_enrollment(config_dir: str | Path) -> HostedEnrollment | None:
    path = hosted_config_path(config_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    fields = {k: data.get(k) for k in HostedEnrollment().__dict__ if data.get(k) is not None}
    return HostedEnrollment(**fields)


def save_enrollment(config_dir: str | Path, enrollment: HostedEnrollment) -> Path:
    """Write the managed hosted config atomically with 0600 permissions."""
    directory = Path(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = hosted_config_path(directory)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(enrollment.to_public_dict(), indent=2, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path
