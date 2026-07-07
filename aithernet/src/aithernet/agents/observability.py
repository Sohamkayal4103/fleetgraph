"""Secret-free provider observability records (beta.4).

Each provider invocation produces a normalized, credential-free record for diagnostics, the portal,
and the research datasets. We record only what is observable — the authentication CATEGORY (never a
credential), the capacity-pool id (non-secret), explicitly reported tokens, finish reason, quota
events, and fallback transitions. Unavailable usage/cost fields are left ``None`` and never
fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProviderInvocation:
    """One provider call, role-aware and credential-free."""

    invocation_id: str
    role: str                       # coordinator | coding
    provider: str
    adapter: str                    # cli | api | local | cloud
    auth_category: str              # subscription | api | cloud | local (NEVER a credential)
    capacity_pool_id: str
    requested_model: str | None = None
    effective_model: str | None = None
    session_id: str | None = None   # safe provider session id, where exposed
    started_at: str | None = None
    ended_at: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None         # only if the provider reports it
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    quota_event: str | None = None          # canonical category, e.g. quota_exhausted
    fallback_from: str | None = None        # the entry this invocation fell back from, if any
    mission_id: str | None = None
    mission_result: str | None = None
    extra: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        """A secret-free dataset record (record_type ``provider_invocation``)."""
        return {
            "record_type": "provider_invocation",
            "mission_id": self.mission_id or self.invocation_id,
            "invocation_id": self.invocation_id,
            "role": self.role,
            "provider": self.provider,
            "adapter": self.adapter,
            "auth_category": self.auth_category,
            "capacity_pool_id": self.capacity_pool_id,
            "requested_model": self.requested_model,
            "effective_model": self.effective_model,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "finish_reason": self.finish_reason,
            "quota_event": self.quota_event,
            "fallback_from": self.fallback_from,
            "mission_result": self.mission_result,
            **{k: v for k, v in self.extra.items()
               if "secret" not in k.lower() and "key" not in k.lower() and "token" != k.lower()},
        }


def quota_event_record(*, role: str, provider: str, capacity_pool_id: str, category: str,
                       retry_after_seconds: int | None = None, limit_scope: str | None = None,
                       mission_id: str | None = None) -> dict:
    """A secret-free quota/rate-limit event record (record_type ``provider_quota_event``)."""
    return {
        "record_type": "provider_quota_event",
        "mission_id": mission_id or capacity_pool_id,
        "role": role,
        "provider": provider,
        "capacity_pool_id": capacity_pool_id,
        "category": category,
        "retry_after_seconds": retry_after_seconds,
        "limit_scope": limit_scope,
    }
