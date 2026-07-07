"""Training-trajectory payload schemas (Stage 14E, Part 19).

Builder functions that produce sanitized, bounded payload dicts for model-training records.
Deterministic action FACTS are kept separate from natural-language EXPLANATIONS. Hidden chain
of thought / provider-private reasoning is NEVER included, and internal reasoning is never
inferred from provider logs. These dicts still pass through the redactor before sealing.

Schemas (each a ``kind`` discriminator inside the training category):
    MissionTrajectoryRecord, CoordinatorDecisionRecord, CodingTaskRecord,
    HardwareActionRecord, RecoveryDecisionRecord, HumanCorrectionRecord, OutcomeRecord
"""

from __future__ import annotations

_MAX_TEXT = 4000


def _clip(text: str | None, limit: int = _MAX_TEXT) -> str | None:
    if text is None:
        return None
    return text[:limit]


def mission_trajectory(
    *, objective: str, context: dict | None = None, capabilities: list | None = None,
    chosen_route: str | None = None, action: dict | None = None, result: dict | None = None,
    recovery: dict | None = None, outcome: str | None = None, correction: str | None = None,
    accepted: bool | None = None, artifact_metadata: list | None = None,
) -> dict:
    return {
        "kind": "mission_trajectory",
        "objective": _clip(objective),
        "context": context or {},
        "capabilities": (capabilities or [])[:128],
        "chosen_route": chosen_route,
        "action": action or {},
        "result": result or {},
        "recovery": recovery or {},
        "outcome": outcome,
        "user_correction": _clip(correction),
        "accepted": accepted,
        "artifact_metadata": (artifact_metadata or [])[:64],
    }


def coordinator_decision(
    *, objective: str, available_capabilities: list | None = None, route_target: str,
    action: str, structured_action: dict | None = None, confidence: float | None = None,
    explanation: str | None = None,
) -> dict:
    # Deterministic facts (route/action/structured) are kept apart from the NL explanation.
    return {
        "kind": "coordinator_decision",
        "objective": _clip(objective),
        "available_capabilities": (available_capabilities or [])[:128],
        "facts": {"route_target": route_target, "action": action,
                  "structured_action": structured_action or {}},
        "confidence": confidence,
        "explanation": _clip(explanation),
    }


def coding_task(
    *, objective: str, available_tools: list | None = None, patch_metadata: dict | None = None,
    test_result: dict | None = None, outcome: str | None = None,
) -> dict:
    return {
        "kind": "coding_task",
        "objective": _clip(objective),
        "available_tools": (available_tools or [])[:64],
        "patch_metadata": patch_metadata or {},
        "test_result": test_result or {},
        "outcome": outcome,
    }


def hardware_action(
    *, device_family: str | None = None, capability_facts: dict | None = None,
    selected_backend: str | None = None, action: dict | None = None, result: dict | None = None,
) -> dict:
    return {
        "kind": "hardware_action",
        "device_family": device_family,
        "capability_facts": capability_facts or {},
        "selected_backend": selected_backend,
        "action": action or {},
        "result": result or {},
    }


def recovery_decision(
    *, failure_category: str, attempt: int, action: dict | None = None,
    succeeded: bool | None = None,
) -> dict:
    return {
        "kind": "recovery_decision",
        "failure_category": failure_category,
        "attempt": attempt,
        "action": action or {},
        "succeeded": succeeded,
    }


def human_correction(
    *, correction: str, accepted: bool | None = None, rejected: bool | None = None,
) -> dict:
    return {
        "kind": "human_correction",
        "correction": _clip(correction),
        "accepted": accepted,
        "rejected": rejected,
    }


def outcome(
    *, mission_outcome: str, success: bool | None = None, uncertainty: float | None = None,
) -> dict:
    return {
        "kind": "outcome",
        "mission_outcome": mission_outcome,
        "success": success,
        "uncertainty": uncertainty,
    }
