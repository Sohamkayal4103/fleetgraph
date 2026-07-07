"""Item 2: persist secret-free RF exchange records into the owner research spool.

Each simulated RF exchange (an :class:`RFExchangeRecord`) is expanded into normalized dataset
records — one per relevant RF dataset class — carrying the mandate's fields (ids, digests, sizes,
profile/modulation/rates, frame count, FEC/CRC, channel params, retries/acks, latency,
decode/signature/replay/delivery outcomes, modem/flowgraph/impl digests, simulated class).
The spool secret-scans before persistence; an RFExchangeRecord never contains key material by
construction. No encryption/signing keys, API credentials, OAuth tokens, or auth headers are stored.
"""

from __future__ import annotations

import hashlib

from aithernet.data.research_spool import ResearchSpool
from aithernet.transport.ota.profiles import RFLinkProfile
from aithernet.transport.ota.simulated_rf import RFExchangeRecord

#: RF dataset record_types produced from one exchange (consumed by the dataset builders).
_RECORD_TYPES = (
    "rf_peer_delivery", "rf_channel_outcome", "rf_frame_recovery", "rf_delivery_outcome",
    "rf_transport_selection", "rf_retransmission",
)


def pseudonym(value: str | None, *, prefix: str = "px") -> str | None:
    """A stable, non-reversible content pseudonym for tenant/mesh/node identifiers in RF datasets.

    Datasets never carry raw emails or tenant names. This is a one-way content hash; with a
    tenant-scoped key the redactor adds unlinkability on top. ``None`` passes through as ``None``.
    """
    if value is None:
        return None
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def rf_exchange_to_records(record: RFExchangeRecord, *, profile: RFLinkProfile | None = None,
                           mission_id: str | None = None, run_id: str | None = None,
                           transport_class: str = "simulated") -> list[dict]:
    """Expand one RF exchange into normalized dataset records (one per RF dataset class)."""
    base = {
        "mission_id": mission_id or record.message_id,   # mission-grouped splitting key
        "run_id": run_id,
        "message_id": record.message_id,
        "sender_node_id": record.sender_node_id,
        "receiver_node_id": record.receiver_node_id,
        "canonical_payload_digest": record.canonical_payload_digest,
        "received_payload_digest": record.received_payload_digest,
        "serialized_size": record.serialized_size,
        "encrypted_size": record.encrypted_size,
        "profile_id": record.profile_id,
        "modulation": record.modulation,
        "symbol_rate": profile.symbol_rate if profile else None,
        "sample_rate": profile.sample_rate if profile else None,
        "occupied_bandwidth_hz": profile.occupied_bandwidth_hz if profile else None,
        "fec_scheme": profile.fec_scheme if profile else None,
        "crc_scheme": profile.crc_scheme if profile else None,
        "frame_count": record.frame_count,
        "retries": record.retries,
        "acknowledgements": record.acknowledgements,
        "delivery_latency_ms": record.delivery_latency_ms,
        "channel": record.channel,
        "decode_outcome": record.decode_outcome,
        "signature_result": record.signature_result,
        "replay_result": record.replay_result,
        "transport_class": transport_class,   # simulated | cabled | shielded | ota
        "simulated": record.simulated,
    }
    return [{**base, "record_type": rt} for rt in _RECORD_TYPES]


def persist_rf_exchange(record: RFExchangeRecord, *, spool: ResearchSpool | None = None,
                        profile: RFLinkProfile | None = None, mission_id: str | None = None,
                        run_id: str | None = None) -> list[dict]:
    """Secret-scan + persist an RF exchange into the research spool (normalized stage)."""
    sp = spool or ResearchSpool()
    receipts = []
    for rec in rf_exchange_to_records(record, profile=profile, mission_id=mission_id,
                                      run_id=run_id):
        receipts.append(sp.write_normalized_record(rec))
    return receipts


def physical_tx_to_record(tx_record, *, plan, config, transport_class: str,
                          mission_id: str | None = None, snr_db=None, evm=None, ber=None,
                          frame_error_rate=None) -> dict:
    """Build a secret-free PHYSICAL RF exchange record from an executor result + plan + config.

    ``transport_class`` is cabled | shielded | ota (or loopback for a contained self-test). Never
    includes encryption/signing keys, credentials, or auth headers — only the operator-supplied
    physical parameters + measured link metrics."""
    from dataclasses import asdict
    return {
        "record_type": "rf_physical_tx",
        "mission_id": mission_id or plan.device_id,
        "requested_transport": "rf_ota", "selected_transport": "rf_ota",
        "sender_device_id": plan.device_id, "uri": plan.uri, "tx_channel": plan.channel,
        "antenna": config.antenna, "profile_id": plan.profile_id,
        "profile_digest": plan.profile_digest, "implementation_digest": plan.implementation_digest,
        "flowgraph_digest": tx_record.flowgraph_digest, "plan_digest": tx_record.plan_digest,
        "center_frequency_hz": plan.center_frequency_hz, "sample_rate": plan.sample_rate,
        "occupied_bandwidth_hz": plan.occupied_bandwidth_hz, "gain": plan.gain,
        "duration_seconds": plan.duration_seconds, "message_count": plan.message_count,
        "frame_count": plan.frame_count, "operating_region_label": config.operating_region_label,
        "allowed": tx_record.allowed, "radiated": tx_record.radiated,
        "backend": tx_record.backend, "snr_db": snr_db, "evm": evm, "ber": ber,
        "frame_error_rate": frame_error_rate, "transport_class": transport_class,
        "executor_metadata": tx_record.metadata,
        "operator_config": {k: v for k, v in asdict(config).items()
                            if "secret" not in k and "key" not in k},
    }


#: MAC-transfer record_types produced from one MAC transfer result.
_MAC_RECORD_TYPES = (
    "rf_channel_access", "rf_rts_cts_outcome", "rf_collision_recovery", "rf_retransmission",
    "rf_delivery_latency",
)


def mac_transfer_to_records(
    result, *, tenant_id: str | None = None, mesh_id: str | None = None,
    sender_node_id: str | None = None, receiver_node_id: str | None = None,
    mission_id: str | None = None, run_id: str | None = None, transport_class: str = "simulated",
    mission_ack: str | None = None, mission_result: str | None = None,
) -> list[dict]:
    """Expand one MAC transfer (:class:`...mac.MacTransferResult`) into normalized dataset records.

    Carries the beta.3 MAC fields — MAC mode, RTS/CTS timing, reservation, contention/backoff,
    frame acknowledgements, retransmissions, the link-ACK and delivery-receipt layers — plus
    pseudonymous tenant/mesh/node ids and (separately) the mission-ack / mission-result layers so
    a radio frame ACK is never conflated with mission success. Secret-free by construction.
    """
    base = {
        "mission_id": mission_id or str(result.session_id),
        "run_id": run_id,
        "session_id": result.session_id,
        "tenant_pseudonym": pseudonym(tenant_id),
        "mesh_pseudonym": pseudonym(mesh_id),
        "sender_pseudonym": pseudonym(sender_node_id),
        "receiver_pseudonym": pseudonym(receiver_node_id),
        "mac_mode": result.mac_mode,
        "final_state": result.final_state,
        "handshake_attempts": result.handshake_attempts,
        "rts_sent": result.rts_sent,
        "cts_received": result.cts_received,
        "cts_rejected": result.cts_rejected,
        "reservation_us": result.reservation_us,
        "rts_cts_turnaround_us": result.rts_cts_turnaround_us,
        "data_frames_sent": result.data_frames_sent,
        "retransmissions": result.retransmissions,
        "retransmit_rounds": result.retransmit_rounds,
        "block_acks": result.block_acks,
        "nacks": result.nacks,
        "duplicates_suppressed": result.duplicates_suppressed,
        "backoff_events": result.backoff_events,
        "backoff_total_us": result.backoff_total_us,
        "collisions": result.collisions,
        "carrier_busy_deferrals": result.carrier_busy_deferrals,
        "cancelled": result.cancelled,
        # Four distinct acknowledgement layers — never collapsed into one.
        "link_acknowledged": result.link_acknowledged,
        "delivery_receipt": result.delivery_receipt,
        "mission_ack": mission_ack,
        "mission_result": mission_result,
        "delivered": result.delivered,
        "total_airtime_us": result.total_airtime_us,
        "transport_class": transport_class,
        "simulated": transport_class in ("simulated", "loopback"),
    }
    return [{**base, "record_type": rt} for rt in _MAC_RECORD_TYPES]


def mesh_decision_to_record(
    *, decision: str, reason: str, tenant_id: str | None, mesh_id: str | None,
    sender_node_id: str | None, receiver_node_id: str | None, mission_id: str | None = None,
    cross_tenant: bool = False, transport_class: str = "simulated",
) -> dict:
    """A secret-free mesh-authorization decision record (allow/deny + cross-tenant rejection)."""
    return {
        "record_type": "rf_cross_tenant_rejection" if cross_tenant else "rf_mesh_authorization",
        "mission_id": mission_id or pseudonym(mesh_id) or "mesh",
        "decision": decision,
        "reason": reason,
        "cross_tenant": cross_tenant,
        "tenant_pseudonym": pseudonym(tenant_id),
        "mesh_pseudonym": pseudonym(mesh_id),
        "sender_pseudonym": pseudonym(sender_node_id),
        "receiver_pseudonym": pseudonym(receiver_node_id),
        "transport_class": transport_class,
    }


def fleet_health_to_record(
    *, node_id: str, tenant_id: str | None, mesh_id: str | None, state: str,
    sequence: int | None = None, software_version: str | None = None,
    enabled_transports: list[str] | None = None, mission_id: str | None = None,
) -> dict:
    """A secret-free fleet-health record (heartbeat state for a node, pseudonymized)."""
    return {
        "record_type": "rf_fleet_health",
        "mission_id": mission_id or pseudonym(node_id) or "fleet",
        "node_pseudonym": pseudonym(node_id),
        "tenant_pseudonym": pseudonym(tenant_id),
        "mesh_pseudonym": pseudonym(mesh_id),
        "fleet_state": state,
        "heartbeat_sequence": sequence,
        "software_version": software_version,
        "enabled_transports": list(enabled_transports or []),
    }


def persist_mac_transfer(result, *, spool: ResearchSpool | None = None, **kwargs) -> list[dict]:
    """Secret-scan + persist a MAC transfer's normalized records into the research spool."""
    sp = spool or ResearchSpool()
    return [sp.write_normalized_record(rec) for rec in mac_transfer_to_records(result, **kwargs)]


def persist_physical_tx(tx_record, *, plan, config, transport_class: str,
                        spool: ResearchSpool | None = None, mission_id: str | None = None,
                        **metrics) -> dict:
    """Secret-scan + persist a physical TX execution record into the research spool."""
    sp = spool or ResearchSpool()
    rec = physical_tx_to_record(tx_record, plan=plan, config=config,
                                transport_class=transport_class, mission_id=mission_id, **metrics)
    return sp.write_normalized_record(rec)
