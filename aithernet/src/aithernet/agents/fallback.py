"""Explicit provider fallback chains (beta.4).

A role may declare an ORDERED fallback chain. Fallback is always **explicit** (only configured
providers are ever used), always **recorded** (every transition is logged with provenance), and
never silent. A fallback provider is built from its OWN configuration and secret reference, so one
provider's credentials are never used to call another. Fallback is triggered ONLY by known
provider-availability conditions (quota exhausted, rate limited, subscription/credit unavailable,
endpoint/model unavailable) — those conditions do not burn the mission's ordinary failure budget,
and an ordinary error (bad request, malformed output, workspace violation, …) never triggers a
provider change.
"""

from __future__ import annotations

from dataclasses import dataclass

from aithernet.agents import capabilities as caps

#: Canonical error categories that justify moving to the next provider in the chain. These are
#: provider-availability conditions, NOT ordinary failures — they must not consume the failure
#: budget and must not trigger a retry storm against the same provider.
FALLBACK_CATEGORIES = frozenset({
    "quota_exhausted",
    "rate_limited",
    "subscription_unavailable",
    "api_billing_required",
    "credit_exhausted",
    "endpoint_unavailable",
    "model_unavailable",
    "provider_internal_error",
})


def split_entry(entry: str) -> tuple[str, str | None]:
    """Split a chain entry ``provider`` or ``provider:profile`` into (provider_key, profile)."""
    entry = (entry or "").strip()
    if ":" in entry:
        provider, profile = entry.split(":", 1)
        return provider.strip(), profile.strip() or None
    return entry, None


def resolve_chain(role: str, primary: str, fallbacks: list[str] | None) -> list[str]:
    """Return the ordered, validated provider chain for a role.

    The chain is ``[primary, *fallbacks]`` with: blanks removed, duplicates removed (first wins),
    ``disabled`` excluded, and every entry required to satisfy the role's capability requirement
    (a provider that cannot perform the role is never silently inserted into the chain). Entries
    may be ``provider`` or ``provider:profile`` — capability is checked on the provider key.
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in [primary, *(fallbacks or [])]:
        entry = (entry or "").strip()
        if not entry:
            continue
        provider_key, _ = split_entry(entry)
        if provider_key in ("", "disabled"):
            continue
        if not caps.satisfies_role(provider_key, role):
            continue  # never put a role-incapable provider in the chain
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return out


def should_fallback(error_category: str | None) -> bool:
    """True iff ``error_category`` is a provider-availability condition that justifies fallback."""
    return error_category in FALLBACK_CATEGORIES


@dataclass(frozen=True)
class FallbackTransition:
    """A recorded, secret-free provider transition (provenance preserved)."""

    role: str
    from_entry: str
    to_entry: str
    reason_category: str
    invocation_id: str | None = None

    def record(self) -> dict:
        from_p, from_profile = split_entry(self.from_entry)
        to_p, to_profile = split_entry(self.to_entry)
        return {
            "event": "provider_fallback",
            "role": self.role,
            "from_provider": from_p,
            "from_profile": from_profile,
            "to_provider": to_p,
            "to_profile": to_profile,
            "reason_category": self.reason_category,
            "invocation_id": self.invocation_id,
            "silent": False,
        }


def next_in_chain(chain: list[str], current_entry: str) -> str | None:
    """The next entry after ``current_entry`` in ``chain``, or None if it is the last/absent."""
    try:
        idx = chain.index(current_entry)
    except ValueError:
        return None
    return chain[idx + 1] if idx + 1 < len(chain) else None
