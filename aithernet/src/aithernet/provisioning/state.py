"""Persistent guided-setup state (Stage 14G, PHASE 4A).

The wizard records every completed step and the operator's selections to a JSON file under the node
state root so it can be **interrupted and resumed**, re-run idempotently, and repaired. The file
holds no secrets — only choices (profile, modes, provider names) and per-step status. Writes are
atomic (temp file + ``os.replace``) so an interrupted write never corrupts the state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

STATE_SCHEMA = 1

#: Per-step lifecycle.
PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
DEFERRED = "deferred"   # planned, but executed by a dedicated command the operator runs later
RUNNING = "running"
INSTALLED = "installed"     # an external install command completed (reconciled from reality)
CONFIGURED = "configured"   # a provider/selection was configured (reconciled from reality)
VERIFIED = "verified"       # reconciled + verified against the live system
BLOCKED = "blocked"


def default_state_root() -> Path:
    """Node state root — shared with the hosted CLI (``AITHERNET_STATE_ROOT`` override)."""
    return Path(os.environ.get("AITHERNET_STATE_ROOT",
                               str(Path.home() / ".local/share/aithernet")))


def state_path(state_root: Path | None = None) -> Path:
    return (state_root or default_state_root()) / "setup" / "state.json"


@dataclass
class StepRecord:
    status: str = PENDING
    detail: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail, "updated_at": self.updated_at}


@dataclass
class SetupState:
    """The recorded answers + per-step progress for a guided setup run."""

    schema: int = STATE_SCHEMA
    # operator selections (no secrets)
    deployment_mode: str = ""        # standalone | hosted
    identity_model: str = ""         # workstation | appliance
    node_name: str = ""
    node_id: str = ""                # stable canonical node id (written into node.yaml)
    hardware_profile: str = ""       # customer-facing name
    sdr_strategy: str = ""           # recommended | source | offline | none
    service_mode: str = ""           # systemd-user | systemd-system | manual
    coordinator_provider: str = ""   # provider key or "disabled"
    coding_provider: str = ""        # provider key or "disabled"
    components: list[str] = field(default_factory=list)
    steps: dict[str, StepRecord] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    # -- step helpers -------------------------------------------------------
    def record(self, step_id: str, status: str, detail: str = "", *, now: str = "") -> None:
        self.steps[step_id] = StepRecord(status=status, detail=detail, updated_at=now)
        self.updated_at = now

    def status_of(self, step_id: str) -> str:
        rec = self.steps.get(step_id)
        return rec.status if rec else PENDING

    def is_complete(self, step_id: str) -> bool:
        return self.status_of(step_id) in (
            DONE, SKIPPED, DEFERRED, INSTALLED, CONFIGURED, VERIFIED)

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "deployment_mode": self.deployment_mode,
            "identity_model": self.identity_model,
            "node_name": self.node_name,
            "node_id": self.node_id,
            "hardware_profile": self.hardware_profile,
            "sdr_strategy": self.sdr_strategy,
            "service_mode": self.service_mode,
            "coordinator_provider": self.coordinator_provider,
            "coding_provider": self.coding_provider,
            "components": list(self.components),
            "steps": {k: v.to_dict() for k, v in self.steps.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SetupState:
        st = cls(
            schema=int(data.get("schema", STATE_SCHEMA)),
            deployment_mode=data.get("deployment_mode", ""),
            identity_model=data.get("identity_model", ""),
            node_name=data.get("node_name", ""),
            node_id=data.get("node_id", ""),
            hardware_profile=data.get("hardware_profile", ""),
            sdr_strategy=data.get("sdr_strategy", ""),
            service_mode=data.get("service_mode", ""),
            coordinator_provider=data.get("coordinator_provider", ""),
            coding_provider=data.get("coding_provider", ""),
            components=list(data.get("components", [])),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
        for sid, rec in (data.get("steps") or {}).items():
            st.steps[sid] = StepRecord(status=rec.get("status", PENDING),
                                       detail=rec.get("detail", ""),
                                       updated_at=rec.get("updated_at", ""))
        return st


def load_state(state_root: Path | None = None) -> SetupState | None:
    """Load recorded setup state, or ``None`` if no run has started."""
    path = state_path(state_root)
    if not path.is_file():
        return None
    try:
        return SetupState.from_dict(json.loads(path.read_text()))
    except (ValueError, OSError):
        return None


def save_state(state: SetupState, state_root: Path | None = None) -> Path:
    """Persist setup state atomically (0700 dir, 0600 file — choices are private, not secret)."""
    path = state_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path
