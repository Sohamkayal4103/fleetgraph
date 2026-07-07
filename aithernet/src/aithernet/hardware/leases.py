"""Durable hardware leases + RF-backend binding (Stage 14B, Part G/H/I/K/L/R).

Only an ACTIVE valid lease authorizes managed physical-device use. Acquisition is atomic and
conflict-checked (no two conflicting exclusive leases; transmit is never shared; shared receive
only when the device declares it safe). The lease service also owns restart/expiry/orphan
reconciliation, device-disappearance handling, and the deterministic device->backend binding
adapter (the coordinator never supplies device arguments). None of these paths create a MissionStep.
"""

from __future__ import annotations

import contextlib
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

from aithernet.hardware import events as ev
from aithernet.hardware.contracts import (
    BindingNotFoundError,
    DeviceNotFoundError,
    DeviceUnavailableError,
    Direction,
    HealthState,
    LeaseCompatibilityError,
    LeaseConflictError,
    LeaseMode,
    LeaseNotFoundError,
    LeaseState,
    PresenceState,
)
from aithernet.hardware.discovery import sanitize_device_args
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    MissionRepository,
    SDRDeviceBindingRepository,
    SDRDeviceCapabilityRepository,
    SDRDeviceLeaseEventRepository,
    SDRDeviceLeaseRepository,
    SDRDeviceRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

_HEALTH_OK = (HealthState.OK, HealthState.DEGRADED)
_TERMINAL_MISSION = {"completed", "failed", "cancelled", "blocked"}


def _as_aware(value):
    """Coerce a possibly-naive (SQLite-returned) datetime to aware UTC for comparison."""
    from datetime import UTC

    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _selector_matches(device, selector: str) -> bool:
    sel = (selector or "").strip().lower()
    if not sel:
        return False
    if sel == (device.hardware_key or "").lower():
        return True
    if device.serial and sel == device.serial.lower():
        return True
    vp = f"{device.vendor or ''}:{device.product or ''}".lower()
    return sel == vp


class LeaseService:
    """Grants, renews, releases, revokes, and reconciles durable device leases."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.config = runtime.config.hardware
        self._owner = f"hw-lease:{self.node_id}"

    async def _emit(self, event_type: str, *, message: str, payload: dict,
                    mission_id: str | None = None) -> None:
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(
                event_type=event_type, mission_id=mission_id, source="hardware",
                message=message, payload=payload,
            )

    # -- binding adapter ---------------------------------------------------------

    def derive_binding(self, session, device, backend_id: str) -> dict:
        """Derive bounded backend device-arguments for (device, backend) deterministically.

        Order: persisted admin binding -> a matching config binding -> the device's probed
        capability ``device_args``. Raises :class:`BindingNotFoundError` when none exist or the
        backend id is unknown. The coordinator never supplies these arguments.
        """
        if backend_id not in self.runtime.rf.backend_ids():
            raise BindingNotFoundError(f"Unknown RF backend '{backend_id}'.")
        bindings = SDRDeviceBindingRepository(session)
        binding = bindings.get(device_id=device.id, backend_id=backend_id)
        if binding is not None and binding.enabled:
            return sanitize_device_args(binding.device_args_json)
        # Fall back to a matching operator config binding (and persist it for visibility).
        for cfg in self.config.bindings:
            if (cfg.enabled and cfg.backend_id == backend_id
                    and _selector_matches(device, cfg.device_selector)):
                args = sanitize_device_args(cfg.device_args)
                bindings.upsert(node_id=self.node_id, device_id=device.id, backend_id=backend_id,
                                device_args=args, source="config", enabled=True, notes=cfg.notes)
                return args
        caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
        if caps is not None:
            args = sanitize_device_args((caps.capabilities_json or {}).get("device_args") or {})
            if args:
                return args
        raise BindingNotFoundError(
            f"No RF-backend binding for device '{device.id}' and backend '{backend_id}'."
        )

    # -- compatibility checks (Part H) -------------------------------------------

    def _check_compatibility(self, session, device, *, direction: str, channels: list,
                             frequency_hz: float | None, sample_rate: float | None,
                             mode: str, backend_id: str) -> dict:
        if not device.enabled:
            raise DeviceUnavailableError(f"Device '{device.id}' is administratively disabled.")
        if device.presence_state != PresenceState.PRESENT:
            raise DeviceUnavailableError(f"Device '{device.id}' is not present.")
        if device.health_state not in _HEALTH_OK:
            raise DeviceUnavailableError(
                f"Device '{device.id}' health '{device.health_state}' does not permit use."
            )
        caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
        cj = (caps.capabilities_json if caps else {}) or {}
        # direction support (fail only when KNOWN unsupported; unknown stays unknown)
        if direction in (Direction.RX, Direction.RX_TX) and caps and caps.rx_supported is False:
            raise LeaseCompatibilityError(f"Device '{device.id}' does not support RX.")
        if direction in (Direction.TX, Direction.RX_TX) and caps and caps.tx_supported is False:
            raise LeaseCompatibilityError(f"Device '{device.id}' does not support TX.")
        # channel existence
        channel_count = cj.get("channel_count")
        if channels and isinstance(channel_count, int):
            for ch in channels:
                if isinstance(ch, int) and ch >= channel_count:
                    raise LeaseCompatibilityError(
                        f"Device '{device.id}' has no channel {ch} (channels={channel_count})."
                    )
        # frequency support
        if frequency_hz is not None and cj.get("frequency_ranges"):
            ranges = cj["frequency_ranges"]
            if not any(lo <= frequency_hz <= hi for lo, hi in ranges):
                raise LeaseCompatibilityError(
                    f"Frequency {frequency_hz} Hz is outside device '{device.id}' ranges."
                )
        # sample-rate support
        if sample_rate is not None:
            sr_ranges = cj.get("sample_rate_ranges") or []
            sr_values = cj.get("sample_rates") or []
            if sr_ranges or sr_values:
                ok = any(lo <= sample_rate <= hi for lo, hi in sr_ranges) or any(
                    abs(sample_rate - v) < 1e-6 for v in sr_values
                )
                if not ok:
                    raise LeaseCompatibilityError(
                        f"Sample rate {sample_rate} is unsupported by device '{device.id}'."
                    )
        # lease mode rules
        if mode not in (LeaseMode.EXCLUSIVE, LeaseMode.SHARED_RECEIVE):
            raise LeaseCompatibilityError(f"Unsupported lease mode '{mode}'.")
        if mode == LeaseMode.SHARED_RECEIVE:
            if direction != Direction.RX:
                # Transmit (or duplex) is NEVER shared.
                raise LeaseCompatibilityError(
                    "Shared leases are receive-only (no shared transmit)."
                )
            if not (caps and caps.shared_receive_safe):
                raise LeaseCompatibilityError(
                    f"Device '{device.id}' does not declare shared-receive safe."
                )
        # backend binding must exist (raises BindingNotFoundError)
        self.derive_binding(session, device, backend_id)
        return cj

    def capability_view(self, session, device) -> dict:
        """A compact capability summary returned on an incompatible request for reassessment."""
        caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
        cj = (caps.capabilities_json if caps else {}) or {}
        return {
            "device_id": device.id, "rx": cj.get("rx_supported"), "tx": cj.get("tx_supported"),
            "channels": cj.get("channel_count"), "frequency_ranges": cj.get("frequency_ranges"),
            "sample_rate_ranges": cj.get("sample_rate_ranges"),
            "shared_receive_safe": cj.get("shared_receive_safe"),
            "present": device.presence_state == PresenceState.PRESENT,
            "enabled": device.enabled, "health": device.health_state,
        }

    # -- acquire -----------------------------------------------------------------

    async def acquire(
        self, device_id: str, *, backend_id: str, direction: str = Direction.RX,
        mode: str = LeaseMode.EXCLUSIVE, operation: str | None = None,
        channels: list | None = None, frequency_hz: float | None = None,
        sample_rate: float | None = None, mission_id: str | None = None,
        mission_run_id: str | None = None, mission_step_id: str | None = None,
        duration_seconds: float | None = None,
    ) -> str:
        """Validate + atomically grant a lease. Returns the lease id. Raises on failure/conflict."""
        channels = channels or []
        duration = min(
            duration_seconds or self.config.lease_duration_seconds,
            self.config.max_lease_duration_seconds,
        )
        now = utcnow()
        expiry = now + timedelta(seconds=duration)
        # 1) validate + create pending lease in one transaction
        with self.runtime.session_scope() as session:
            device = SDRDeviceRepository(session).get(device_id)
            if device is None or device.node_id != self.node_id:
                raise DeviceNotFoundError(f"Unknown device '{device_id}'.")
            self._check_compatibility(
                session, device, direction=direction, channels=channels,
                frequency_hz=frequency_hz, sample_rate=sample_rate, mode=mode,
                backend_id=backend_id,
            )
            leases = SDRDeviceLeaseRepository(session)
            lease = leases.create(
                node_id=self.node_id, device_id=device_id, backend_id=backend_id,
                mission_id=mission_id, mission_run_id=mission_run_id,
                mission_step_id=mission_step_id, requested_operation=(operation or "")[:64] or None,
                direction=direction, requested_channels_json=channels, lease_mode=mode,
                state=LeaseState.PENDING,
            )
            lease_id = lease.id
            SDRDeviceLeaseEventRepository(session).create(
                lease_id=lease_id, device_id=device_id, node_id=self.node_id,
                event_type="requested", state=LeaseState.PENDING,
            )
            session.commit()
        await self._emit(ev.EVENT_LEASE_REQUESTED, message=f"lease requested for {device_id}",
                         payload={"lease_id": lease_id, "device_id": device_id,
                                  "backend_id": backend_id, "mode": mode, "direction": direction},
                         mission_id=mission_id)
        # 2) atomic, conflict-checked grant
        token = secrets.token_hex(16)
        with self.runtime.session_scope() as session:
            leases = SDRDeviceLeaseRepository(session)
            granted = leases.grant(
                lease_id, device_id=device_id, mode=mode, owner_worker_id=self._owner,
                owner_node_id=self.node_id, token=token, now=now, expiry=expiry,
            )
            if granted:
                SDRDeviceLeaseEventRepository(session).create(
                    lease_id=lease_id, device_id=device_id, node_id=self.node_id,
                    event_type="acquired", state=LeaseState.ACTIVE,
                )
            else:
                leases.transition(lease_id, to_state=LeaseState.FAILED,
                                  reason="conflicting active lease", from_states=("pending",))
                SDRDeviceLeaseEventRepository(session).create(
                    lease_id=lease_id, device_id=device_id, node_id=self.node_id,
                    event_type="failed", state=LeaseState.FAILED, reason="conflict",
                )
            session.commit()
        if not granted:
            await self._emit(ev.EVENT_LEASE_FAILED, message=f"lease conflict for {device_id}",
                             payload={"lease_id": lease_id, "device_id": device_id,
                                      "reason": "conflict"}, mission_id=mission_id)
            raise LeaseConflictError(
                f"Device '{device_id}' already has a conflicting active lease."
            )
        await self._emit(ev.EVENT_LEASE_ACQUIRED, message=f"lease acquired for {device_id}",
                         payload={"lease_id": lease_id, "device_id": device_id,
                                  "backend_id": backend_id, "expires_at": expiry.isoformat()},
                         mission_id=mission_id)
        return lease_id

    # -- renew / release / revoke ------------------------------------------------

    async def renew(self, lease_id: str) -> bool:
        """Renew an active lease (infrastructure; creates NO MissionStep)."""
        now = utcnow()
        with self.runtime.session_scope() as session:
            leases = SDRDeviceLeaseRepository(session)
            lease = leases.get(lease_id)
            if lease is None:
                raise LeaseNotFoundError(f"Unknown lease '{lease_id}'.")
            if lease.state != LeaseState.ACTIVE or not lease.lease_token:
                return False
            expiry = now + timedelta(seconds=self.config.lease_duration_seconds)
            ok = leases.renew(lease_id, token=lease.lease_token, now=now, expiry=expiry)
            mission_id = lease.mission_id
            device_id = lease.device_id
            session.commit()
        if ok:
            await self._emit(ev.EVENT_LEASE_RENEWED, message=f"lease renewed {lease_id}",
                             payload={"lease_id": lease_id, "device_id": device_id},
                             mission_id=mission_id)
        return bool(ok)

    async def release(self, lease_id: str, *, reason: str = "released") -> bool:
        return await self._terminate(lease_id, to_state=LeaseState.RELEASED,
                                     event=ev.EVENT_LEASE_RELEASED, reason=reason)

    async def revoke(self, lease_id: str, *, reason: str = "operator revoked") -> bool:
        return await self._terminate(lease_id, to_state=LeaseState.REVOKED,
                                     event=ev.EVENT_LEASE_REVOKED, reason=reason)

    async def _terminate(self, lease_id: str, *, to_state: str, event: str, reason: str) -> bool:
        with self.runtime.session_scope() as session:
            leases = SDRDeviceLeaseRepository(session)
            lease = leases.get(lease_id)
            if lease is None:
                raise LeaseNotFoundError(f"Unknown lease '{lease_id}'.")
            if lease.state in ("released", "expired", "revoked", "failed"):
                return False
            moved = leases.transition(lease_id, to_state=to_state, reason=reason[:320],
                                      released=True)
            if moved:
                SDRDeviceLeaseEventRepository(session).create(
                    lease_id=lease_id, device_id=lease.device_id, node_id=self.node_id,
                    event_type=to_state, state=to_state, reason=reason[:320],
                )
            mission_id = lease.mission_id
            device_id = lease.device_id
            session.commit()
        if moved:
            await self._emit(event, message=f"lease {to_state} {lease_id}",
                             payload={"lease_id": lease_id, "device_id": device_id,
                                      "reason": reason}, mission_id=mission_id)
        return bool(moved)

    async def release_for_mission(
        self, mission_id: str, *, reason: str = "mission terminal"
    ) -> int:
        """Release every holding lease tied to a mission (terminal-state / cancellation release)."""
        with self.runtime.session_scope() as session:
            leases = SDRDeviceLeaseRepository(session)
            holding = leases.list(node_id=self.node_id, mission_id=mission_id, active_only=True)
            ids = [(le.id, le.device_id) for le in holding]
            session.commit()
        released = 0
        for lease_id, _device_id in ids:
            with contextlib.suppress(Exception):
                if await self.release(lease_id, reason=reason):
                    released += 1
        return released

    # -- device disappearance (Part L) -------------------------------------------

    async def on_device_missing(self, device_id: str, *, reason: str) -> None:
        """A leased device disappeared: orphan its holding leases + notify linked missions.

        The lease is transitioned to ``orphaned`` (it no longer safely holds the device) and a
        factual persisted event references the linked mission so the coordinator can reassess. A
        missing device NEVER invents mission completion.
        """
        now = utcnow()
        with self.runtime.session_scope() as session:
            leases = SDRDeviceLeaseRepository(session)
            holding = leases.holding_for_device(device_id)
            affected = []
            for lease in holding:
                lease.state = LeaseState.ORPHANED
                lease.reason = f"device missing: {reason}"[:320]
                lease.expires_at = now + timedelta(seconds=self.config.stale_lease_grace_seconds)
                SDRDeviceLeaseEventRepository(session).create(
                    lease_id=lease.id, device_id=device_id, node_id=self.node_id,
                    event_type="orphaned", state=LeaseState.ORPHANED, reason="device missing",
                )
                affected.append((lease.id, lease.mission_id))
            session.commit()
        for lease_id, mission_id in affected:
            await self._emit(ev.EVENT_LEASE_ORPHANED,
                             message=f"lease orphaned (device missing) {lease_id}",
                             payload={"lease_id": lease_id, "device_id": device_id,
                                      "reason": "device_missing"}, mission_id=mission_id)

    # -- restart recovery + periodic reconciliation (Part R) ---------------------

    def recover(self) -> None:
        """On startup, orphan every holding lease from the previous process (no token can match).

        An orphaned lease still BLOCKS a new conflicting grant until reconciliation expires it, so
        a restart never silently grants a conflicting lease before stale-lease reconciliation. The
        orphaned lease's expiry is shortened to the stale grace so the device frees up promptly.
        """
        now = utcnow()
        grace = timedelta(seconds=self.config.stale_lease_grace_seconds)
        with self.runtime.session_scope() as session:
            leases = SDRDeviceLeaseRepository(session)
            for lease in leases.holding_all(node_id=self.node_id):
                lease.state = LeaseState.ORPHANED
                lease.lease_token = None
                lease.reason = "node restarted; lease orphaned pending reconciliation"
                lease.expires_at = now + grace
                SDRDeviceLeaseEventRepository(session).create(
                    lease_id=lease.id, device_id=lease.device_id, node_id=self.node_id,
                    event_type="orphaned", state=LeaseState.ORPHANED, reason="node restarted",
                )
            session.commit()

    async def reconcile(self) -> None:
        """Expire orphaned/expired leases and release leases of terminal missions."""
        now = utcnow()
        emit: list = []
        with self.runtime.session_scope() as session:
            leases = SDRDeviceLeaseRepository(session)
            events = SDRDeviceLeaseEventRepository(session)
            missions = MissionRepository(session)
            # active past expiry -> orphaned (still blocks; visible)
            for lease in leases.expired_active(now):
                lease.state = LeaseState.ORPHANED
                lease.reason = "lease expired without renewal"
                events.create(lease_id=lease.id, device_id=lease.device_id, node_id=self.node_id,
                              event_type="orphaned", state=LeaseState.ORPHANED, reason="expired")
                emit.append((ev.EVENT_LEASE_ORPHANED, lease.id, lease.device_id, lease.mission_id))
            session.commit()
            # orphaned past expiry -> expired + released (device frees)
            for lease in leases.list(node_id=self.node_id, limit=1000):
                if lease.state == LeaseState.ORPHANED and (
                    lease.expires_at is None or _as_aware(lease.expires_at) < now
                ):
                    leases.transition(lease.id, to_state=LeaseState.EXPIRED,
                                      reason="orphaned lease expired", released=True)
                    events.create(lease_id=lease.id, device_id=lease.device_id,
                                  node_id=self.node_id, event_type="expired",
                                  state=LeaseState.EXPIRED, reason="orphaned -> expired")
                    emit.append((ev.EVENT_LEASE_EXPIRED, lease.id, lease.device_id,
                                 lease.mission_id))
            session.commit()
            # holding leases whose mission is terminal -> released
            terminal_ids = []
            for lease in leases.holding_all(node_id=self.node_id):
                if lease.mission_id:
                    mission = missions.get(lease.mission_id)
                    if mission is not None and mission.status in _TERMINAL_MISSION:
                        terminal_ids.append(lease.id)
            session.commit()
        for lease_id in terminal_ids:
            with contextlib.suppress(Exception):
                await self.release(lease_id, reason="mission reached terminal state")
        for event_type, lease_id, device_id, mission_id in emit:
            await self._emit(event_type, message=f"{event_type} {lease_id}",
                             payload={"lease_id": lease_id, "device_id": device_id},
                             mission_id=mission_id)
