"""Managed hardware inventory service + background worker (Stage 14B, Part F/L).

The inventory service runs the enabled discovery providers, persists factual devices, refreshes
capabilities, updates presence/health, records disappeared/reappeared devices, and emits sanitized
events. It is INDEPENDENT of the mission worker, uses bounded polling, survives provider failure,
and never creates a MissionStep. Discovery output is untrusted; every persisted field is bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from aithernet.hardware import events as ev
from aithernet.hardware.contracts import (
    DeviceCapabilities,
    DeviceHealth,
    DeviceStatus,
    HealthState,
    PresenceState,
)
from aithernet.hardware.discovery import sanitize_device_args
from aithernet.ops.readiness import ComponentState
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    SDRDeviceCapabilityRepository,
    SDRDeviceHealthEventRepository,
    SDRDeviceRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

READINESS_INVENTORY = "hardware_inventory"


def _status_for(*, enabled: bool, presence: str, health: str) -> str:
    if not enabled:
        return DeviceStatus.DISABLED
    if presence == PresenceState.MISSING:
        return DeviceStatus.MISSING
    if health == HealthState.ERROR:
        return DeviceStatus.ERROR
    if health == HealthState.DEGRADED:
        return DeviceStatus.DEGRADED
    return DeviceStatus.PRESENT


def _caps_to_json(caps: DeviceCapabilities) -> dict:
    return {
        "rx_supported": caps.rx_supported,
        "tx_supported": caps.tx_supported,
        "full_duplex": caps.full_duplex,
        "channel_count": caps.channel_count,
        "channel_directions": caps.channel_directions,
        "frequency_ranges": [[r.min_hz, r.max_hz] for r in caps.frequency_ranges],
        "sample_rate_ranges": [[r.min, r.max] for r in caps.sample_rate_ranges],
        "sample_rates": caps.sample_rates,
        "gain_ranges": [[r.min, r.max] for r in caps.gain_ranges],
        "gain_stages": caps.gain_stages,
        "antennas": caps.antennas,
        "bandwidth_ranges": [[r.min, r.max] for r in caps.bandwidth_ranges],
        "clock_sources": caps.clock_sources,
        "time_sources": caps.time_sources,
        "hardware_timestamp": caps.hardware_timestamp,
        "stream_formats": caps.stream_formats,
        "driver": caps.driver,
        "device_args": sanitize_device_args(caps.device_args),
        "shared_receive_safe": caps.shared_receive_safe,
        "extra": caps.extra or {},
    }


class InventoryService:
    """Discovers, persists, and maintains the factual managed-hardware inventory."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.config = runtime.config.hardware
        self.registry = runtime.hardware_discovery
        self.leases = None  # wired by the runtime after the lease service is built

    # -- emit helper -------------------------------------------------------------

    async def _emit(self, event_type: str, *, message: str, payload: dict,
                    mission_id: str | None = None) -> None:
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(
                event_type=event_type, mission_id=mission_id, source="hardware",
                message=message, payload=payload,
            )

    # -- startup recovery --------------------------------------------------------

    def recover(self) -> None:
        """Reset persisted presence after restart (not assumed present until rediscovery).

        Persisted device records (capabilities, history, identities) survive; only the live
        presence is cleared to ``unknown``/``missing`` so a required-device readiness gate and the
        coordinator inventory never claim a device is attached before the first refresh confirms it.
        """
        with self.runtime.session_scope() as session:
            devices = SDRDeviceRepository(session).list(node_id=self.node_id)
            for device in devices:
                device.presence_state = PresenceState.UNKNOWN
                device.status = (
                    DeviceStatus.DISABLED if not device.enabled else DeviceStatus.MISSING
                )
            session.commit()

    # -- refresh -----------------------------------------------------------------

    async def refresh(self, *, force_probe: bool = False) -> dict[str, bool]:
        """Run every enabled provider once (discover, probe, update presence/health).

        ``force_probe`` re-opens every present device for a deep capability probe (used by an
        explicit qualification run); normal polling probes a real device only when it has no
        capabilities yet, and never while it holds a lease.
        """
        availability: dict[str, bool] = {}
        for provider in self.registry.providers():
            availability[provider.backend_id] = await self._refresh_provider(
                provider, force_probe=force_probe)
        return availability

    async def _refresh_provider(self, provider, *, force_probe: bool = False) -> bool:
        provider_id = provider.backend_id
        await self._emit(ev.EVENT_DISCOVERY_STARTED, message=f"discovery started [{provider_id}]",
                         payload={"provider_id": provider_id})
        available = False
        with contextlib.suppress(Exception):
            available = bool(await provider.available())
        if not available:
            await self._emit(ev.EVENT_DISCOVERY_COMPLETED,
                             message=f"provider unavailable [{provider_id}]",
                             payload={"provider_id": provider_id, "available": False, "found": 0})
            return False
        try:
            discovered = await provider.discover()
        except Exception as exc:  # noqa: BLE001 — sanitized; a provider failure never crashes
            await self._emit(ev.EVENT_DISCOVERY_FAILED,
                             message=f"discovery failed [{provider_id}]",
                             payload={"provider_id": provider_id, "error_type": type(exc).__name__})
            return True
        # Decide which devices to PROBE. Probing opens the hardware (expensive), so for a real
        # tool provider we probe only when forced, or when the device has no capabilities yet — and
        # never while it holds a lease (don't disturb a live device).
        probe_always = getattr(provider, "probe_on_every_discovery", True)
        known_caps, leased = self._probe_state(provider_id, discovered)
        probed: list[tuple] = []
        for device in discovered[: self.config.max_devices]:
            held = device.hardware_key in leased
            want_probe = (force_probe or probe_always or device.hardware_key not in known_caps) \
                and not held
            caps = None
            if want_probe:
                with contextlib.suppress(Exception):
                    caps = await provider.probe(device)
            health = DeviceHealth(state=HealthState.UNKNOWN)
            with contextlib.suppress(Exception):
                health = await provider.health(device)
            probed.append((device, caps, health))
        emit_list, missing_device_ids = self._persist(provider_id, probed)
        for item in emit_list:
            await self._emit(item[0], message=item[1], payload=item[2])
        # Notify the lease service about devices that disappeared (handles holding leases).
        if self.leases is not None:
            for device_id in missing_device_ids:
                with contextlib.suppress(Exception):
                    await self.leases.on_device_missing(device_id, reason="device disappeared")
        await self._emit(ev.EVENT_DISCOVERY_COMPLETED,
                         message=f"discovery completed [{provider_id}]",
                         payload={"provider_id": provider_id, "available": True,
                                  "found": len(probed)})
        return True

    def _probe_state(self, provider_id: str, discovered) -> tuple[set[str], set[str]]:
        """Return (keys_with_capabilities, keys_holding_a_lease) for the discovered devices."""
        from aithernet.state.repositories import (
            SDRDeviceCapabilityRepository,
            SDRDeviceLeaseRepository,
        )
        known: set[str] = set()
        leased: set[str] = set()
        keys = {d.hardware_key for d in discovered}
        if not keys:
            return known, leased
        with self.runtime.session_scope() as session:
            device_repo = SDRDeviceRepository(session)
            caps_repo = SDRDeviceCapabilityRepository(session)
            lease_repo = SDRDeviceLeaseRepository(session)
            for key in keys:
                row = device_repo.get_by_key(node_id=self.node_id, hardware_key=key)
                if row is None:
                    continue
                if caps_repo.get_for_device(row.id) is not None:
                    known.add(key)
                if lease_repo.holding_for_device(row.id):
                    leased.add(key)
        return known, leased

    def _persist(self, provider_id: str, probed: list[tuple]) -> tuple[list, list[str]]:
        """Upsert all results in one txn; return (events_to_emit, newly_missing_device_ids)."""
        emit: list = []
        missing_ids: list[str] = []
        now = utcnow()
        seen_keys = {device.hardware_key for device, _, _ in probed}
        with self.runtime.session_scope() as session:
            devices = SDRDeviceRepository(session)
            caps_repo = SDRDeviceCapabilityRepository(session)
            health_repo = SDRDeviceHealthEventRepository(session)
            for device, caps, health in probed:
                row = devices.get_by_key(node_id=self.node_id, hardware_key=device.hardware_key)
                returned = False
                if row is None:
                    row = devices.create(
                        node_id=self.node_id, hardware_key=device.hardware_key,
                        provider_id=provider_id, device_kind=device.device_kind,
                        vendor=device.vendor, product=device.product, serial=device.serial,
                        driver=device.driver, transport=device.transport, channel=device.channel,
                        display_name=device.display_name, identity_limited=device.identity_limited,
                        metadata_json=device.metadata, presence_state=PresenceState.PRESENT,
                        last_seen_at=now,
                    )
                    emit.append((ev.EVENT_DEVICE_DISCOVERED,
                                 f"device discovered: {device.display_name}",
                                 {"device_id": row.id, "provider_id": provider_id,
                                  "device_kind": device.device_kind,
                                  "identity_limited": device.identity_limited}))
                else:
                    returned = row.presence_state != PresenceState.PRESENT
                    row.last_seen_at = now
                    row.presence_state = PresenceState.PRESENT
                    row.display_name = device.display_name or row.display_name
                    row.metadata_json = device.metadata or row.metadata_json
                    if returned:
                        emit.append((ev.EVENT_DEVICE_RETURNED,
                                     f"device returned: {row.display_name}",
                                     {"device_id": row.id, "provider_id": provider_id}))
                    else:
                        emit.append((ev.EVENT_DEVICE_UPDATED,
                                     f"device updated: {row.display_name}",
                                     {"device_id": row.id, "provider_id": provider_id}))
                # capabilities
                if caps is not None:
                    # Enrich the stable record with a serial the probe revealed (the find pass had
                    # none). This does NOT change the hardware_key — identity stays stable.
                    probed_serial = (caps.extra or {}).get("serial")
                    if probed_serial and not row.serial:
                        row.serial = str(probed_serial)[:128]
                    existing = caps_repo.get_for_device(row.id)
                    new_json = _caps_to_json(caps)
                    changed = existing is None or existing.capabilities_json != new_json
                    caps_repo.upsert(device_id=row.id, node_id=self.node_id, fields={
                        "rx_supported": caps.rx_supported, "tx_supported": caps.tx_supported,
                        "full_duplex": caps.full_duplex, "channel_count": caps.channel_count,
                        "shared_receive_safe": caps.shared_receive_safe, "source": caps.source,
                        "confidence": caps.confidence, "capabilities_json": new_json,
                        "probed_at": now,
                    })
                    if changed:
                        row.capability_revision = (row.capability_revision or 0) + 1
                        emit.append((ev.EVENT_CAPABILITIES_UPDATED,
                                     f"capabilities updated: {row.display_name}",
                                     {"device_id": row.id, "rx": caps.rx_supported,
                                      "tx": caps.tx_supported, "channels": caps.channel_count,
                                      "source": caps.source, "confidence": caps.confidence}))
                # health (rate-limited)
                prev_health = row.health_state
                if self._record_health(health_repo, row, health, now):
                    if prev_health != health.state:
                        emit.append((ev.EVENT_DEVICE_HEALTH_CHANGED,
                                     f"health changed: {row.display_name} -> {health.state}",
                                     {"device_id": row.id, "health": health.state,
                                      "previous": prev_health}))
                row.health_state = health.state if returned or caps is not None else health.state
                row.status = _status_for(enabled=row.enabled, presence=row.presence_state,
                                          health=row.health_state)
            # Devices previously present from THIS provider but absent now -> missing.
            for row in devices.list(node_id=self.node_id, provider_id=provider_id):
                if row.hardware_key in seen_keys:
                    continue
                if row.presence_state == PresenceState.PRESENT:
                    row.presence_state = PresenceState.MISSING
                    row.health_state = HealthState.MISSING
                    row.status = _status_for(enabled=row.enabled, presence=PresenceState.MISSING,
                                             health=HealthState.MISSING)
                    health_repo.create(device_id=row.id, node_id=self.node_id,
                                       health_state=HealthState.MISSING,
                                       previous_health_state=None,
                                       presence_state=PresenceState.MISSING,
                                       detail="device no longer discovered")
                    emit.append((ev.EVENT_DEVICE_MISSING, f"device missing: {row.display_name}",
                                 {"device_id": row.id, "provider_id": provider_id}))
                    missing_ids.append(row.id)
            session.commit()
        return emit, missing_ids

    def _record_health(self, health_repo, row, health: DeviceHealth, now) -> bool:
        """Record a health event if state changed or the rate-limit window elapsed."""
        from datetime import UTC

        changed = row.health_state != health.state
        last = row.last_health_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)  # SQLite returns naive datetimes
        within_window = (
            last is not None
            and (now - last).total_seconds() < self.config.health_event_min_interval_seconds
        )
        if not changed and within_window:
            return False
        health_repo.create(
            device_id=row.id, node_id=self.node_id, health_state=health.state,
            previous_health_state=row.health_state, presence_state=row.presence_state,
            detail=(health.detail or "")[:320] or None,
        )
        row.last_health_at = now
        return True


class InventoryWorker:
    """Owns the inventory service's bounded background polling loop (Part F)."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.config = runtime.config.hardware
        self.service: InventoryService = runtime.hardware_inventory
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Recover persisted inventory + reconcile leases, mark readiness, then begin polling."""
        readiness = self.runtime.readiness
        if not self.config.enabled or not self.service.registry.has_providers:
            readiness.mark(READINESS_INVENTORY, ComponentState.DISABLED)
            return
        with contextlib.suppress(Exception):
            self.service.recover()
        with contextlib.suppress(Exception):
            await self.runtime.hardware_leases.recover()
        # Mark required-provider readiness now (before the first refresh completes recovery).
        availability = await self.service.registry.availability()
        self._mark_provider_readiness(availability)
        readiness.mark(READINESS_INVENTORY, ComponentState.READY)
        self._stop.clear()
        self._task = asyncio.ensure_future(self._loop())

    def _mark_provider_readiness(self, availability: dict[str, bool]) -> None:
        for provider_id in self.service.registry.required_ids():
            ok = availability.get(provider_id, False)
            self.runtime.readiness.register(f"hardware_provider:{provider_id}", required=True)
            self.runtime.readiness.mark(
                f"hardware_provider:{provider_id}",
                ComponentState.READY if ok else ComponentState.FAILED,
                detail=None if ok else "required discovery provider unavailable",
            )

    async def _loop(self) -> None:
        # Optional initial discovery shortly after startup (bounded; never blocks start()).
        if self.config.discovery_on_start:
            with contextlib.suppress(Exception):
                await self.service.refresh()
        last_health = 0.0
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self.config.discovery_interval_seconds)
            except TimeoutError:
                pass
            if self._stop.is_set():
                break
            with contextlib.suppress(Exception):
                await self.service.refresh()
            with contextlib.suppress(Exception):
                await self.runtime.hardware_leases.reconcile()
            last_health = loop.time()
            _ = last_health

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            self._task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._task
            self._task = None
