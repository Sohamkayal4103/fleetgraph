"""Capacity pools: the actual account/provider relationship that bounds throughput (beta.4).

A *capacity pool* represents the real allowance a set of invocations draws from. Two roles that
authenticate to the SAME account share ONE pool (e.g. a Claude subscription used for both the
coordinator and Claude Code coding); two roles on different accounts have independent pools. The
pool id is a stable, **non-secret** fingerprint — never a key, token, cookie, or OAuth record.

Pool identity is derived from the provider family + a non-secret discriminator:

    claude_subscription:<runtime-identity-fp>          (claude_cli + claude_code share this)
    chatgpt_codex_subscription:<runtime-identity-fp>
    anthropic_api:<key-ref-fp>                         (fp of the env-var NAME, never the value)
    openai_api:<key-ref-fp>
    aws_bedrock:<region-fp>
    google_project:<project-id>
    google_api:<key-ref-fp>
    local_runtime:<endpoint-fp>
    disabled

The state of a pool (concurrency limit, active/queued counts, quota/rate-limit flags, retry-after)
is tracked here so MissionEngine can queue within a pool, surface a quota-exhausted/rate-limited
state, and recover blocked missions — without inventing usage numbers a provider does not expose.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

#: Provider key -> pool family. Families group providers that draw on the same kind of allowance.
_FAMILY: dict[str, str] = {
    "claude_cli": "claude_subscription",
    "claude_code": "claude_subscription",
    "codex_cli": "chatgpt_codex_subscription",
    "codex_api": "openai_api",
    "anthropic": "anthropic_api",
    "anthropic_bedrock": "aws_bedrock",
    "anthropic_vertex": "google_project",
    "openai_api": "openai_api",
    "openai_compatible": "openai_compatible_endpoint",
    "openai_compatible_local": "local_runtime",
    "gemini_api": "google_api",
    "gemini_vertex": "google_project",
    "gemini_cli": "google_cli",
    "local_coding": "local_runtime",
    "disabled": "disabled",
}

#: Families whose discriminator is the runtime OS identity (subscription CLIs hold no key/endpoint).
_RUNTIME_IDENTITY_FAMILIES = frozenset({
    "claude_subscription", "chatgpt_codex_subscription", "google_cli",
})


def _fp(value: str) -> str:
    """A short, stable, non-reversible fingerprint of a NON-SECRET discriminator."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def pool_family(provider_key: str) -> str:
    """The pool family for a provider key."""
    return _FAMILY.get(provider_key, provider_key or "unknown")


def pool_id(
    provider_key: str,
    *,
    api_key_ref: str = "",
    base_url: str = "",
    project: str = "",
    region: str = "",
    runtime_user: str = "",
) -> str:
    """Compute a stable, non-secret capacity-pool id for a configured provider.

    Inputs are all non-secret: ``api_key_ref`` is the env-var NAME (e.g. ``env:ANTHROPIC_API_KEY``)
    NOT its value; ``base_url`` is an endpoint; ``project``/``region`` are cloud identifiers;
    ``runtime_user`` is the OS user a subscription CLI authenticates as. Never pass a credential.
    """
    family = pool_family(provider_key)
    if family == "disabled":
        return "disabled"
    if family in _RUNTIME_IDENTITY_FAMILIES:
        disc = _fp(runtime_user or "runtime")
    elif family in ("google_project",):
        disc = project or region or _fp(api_key_ref or "google")
    elif family == "aws_bedrock":
        disc = region or _fp(api_key_ref or "aws")
    elif family in ("local_runtime", "openai_compatible_endpoint"):
        disc = _fp(base_url or "local")
    else:  # api families keyed by the key-reference NAME (non-secret)
        disc = _fp(api_key_ref or family)
    return f"{family}:{disc}"


def shared_pool(coordinator_pool: str, coding_pool: str) -> bool:
    """True iff the coordinator and coding roles draw on the SAME capacity pool."""
    return (
        coordinator_pool == coding_pool
        and coordinator_pool not in ("disabled", "")
    )


@dataclass
class PoolState:
    """Mutable runtime state for one capacity pool (no usage numbers are invented)."""

    pool_id: str
    family: str
    concurrency_limit: int = 1
    active: int = 0
    queued: int = 0
    quota_exhausted: bool = False
    rate_limited: bool = False
    retry_after_seconds: int | None = None
    limit_scope: str | None = None      # "session" | "longer_term" where observable, else None
    last_event: str | None = None
    roles: set[str] = field(default_factory=set)

    @property
    def available(self) -> bool:
        return (
            not self.quota_exhausted
            and not self.rate_limited
            and self.active < self.concurrency_limit
        )

    def view(self) -> dict:
        """A bounded, secret-free dict for the CLI / portal / research records."""
        return {
            "pool_id": self.pool_id,
            "family": self.family,
            "roles": sorted(self.roles),
            "concurrency_limit": self.concurrency_limit,
            "active": self.active,
            "queued": self.queued,
            "available": self.available,
            "quota_exhausted": self.quota_exhausted,
            "rate_limited": self.rate_limited,
            "retry_after_seconds": self.retry_after_seconds,
            "limit_scope": self.limit_scope,
            "last_event": self.last_event,
        }


def build_pools(roles: dict[str, dict], *, runtime_user: str = "") -> dict[str, PoolState]:
    """Build the pool map for the configured roles.

    ``roles`` maps role name -> a bounded config dict with keys: provider, api_key_ref, base_url,
    project, region, concurrency_limit. Roles that resolve to the same pool id share ONE PoolState
    (its ``roles`` set lists both), which is exactly the shared-allowance case (Claude coordinator +
    Claude Code coding).
    """
    pools: dict[str, PoolState] = {}
    for role, cfg in roles.items():
        provider = cfg.get("provider", "disabled")
        pid = pool_id(
            provider,
            api_key_ref=cfg.get("api_key_ref", ""),
            base_url=cfg.get("base_url", ""),
            project=cfg.get("project", ""),
            region=cfg.get("region", ""),
            runtime_user=runtime_user,
        )
        if pid == "disabled":
            continue
        state = pools.get(pid)
        if state is None:
            state = PoolState(pool_id=pid, family=pool_family(provider),
                              concurrency_limit=int(cfg.get("concurrency_limit", 1) or 1))
            pools[pid] = state
        else:
            # A shared pool: the smaller configured concurrency bounds the shared allowance.
            state.concurrency_limit = min(
                state.concurrency_limit, int(cfg.get("concurrency_limit", 1) or 1)
            )
        state.roles.add(role)
    return pools
