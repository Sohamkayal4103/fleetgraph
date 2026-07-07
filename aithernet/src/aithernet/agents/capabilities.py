"""Provider capability model + capability-aware role selection (1.0.0-beta.4).

Aithernet is provider-neutral: the coordinator and coding agent are selectable adapters behind
canonical interfaces. This module adds the *capability* layer that makes selection honest and
role-aware — MissionEngine must only select a provider whose declared capabilities satisfy the
requested role, and must never infer coding ability merely because a provider can emit text.

Each provider declares:

* a set of **capabilities** (reasoning, coding, tool_calling, …);
* an **authentication category** (subscription / api / cloud / local / none) — what kind of
  credential it uses, never the credential itself;
* a **billing model** (subscription allowance / separate API billing / cloud billing / local
  compute / none) — so guided setup can state, accurately, who pays for what.

Capabilities are static metadata about the *adapter*, not a runtime probe. Live readiness and
capacity are reported separately (``providers.status`` / ``capacity``). Nothing here reads or
stores a credential.
"""

from __future__ import annotations

#: Capability tokens. ``coding`` / ``workspace_editing`` / ``command_execution`` are what gate a
#: provider into the CODING role; ``reasoning`` / ``structured_decisions`` gate the COORDINATOR
#: role. A text-only model is NOT given ``coding`` — coding is a distinct, declared capability.
REASONING = "reasoning"
STRUCTURED_DECISIONS = "structured_decisions"
TOOL_CALLING = "tool_calling"
CODING = "coding"
WORKSPACE_EDITING = "workspace_editing"
COMMAND_EXECUTION = "command_execution"
STREAMING = "streaming"
USAGE_REPORTING = "usage_reporting"
SESSION_RESUME = "session_resume"
LOCAL_EXECUTION = "local_execution"
SUBSCRIPTION_AUTH = "subscription_auth"
API_AUTH = "api_auth"
CLOUD_AUTH = "cloud_auth"

ALL_CAPABILITIES = frozenset({
    REASONING, STRUCTURED_DECISIONS, TOOL_CALLING, CODING, WORKSPACE_EDITING, COMMAND_EXECUTION,
    STREAMING, USAGE_REPORTING, SESSION_RESUME, LOCAL_EXECUTION, SUBSCRIPTION_AUTH, API_AUTH,
    CLOUD_AUTH,
})

#: The minimum capability each role requires. A provider is eligible for a role only if its
#: capabilities are a superset of the role requirement.
ROLE_REQUIREMENTS: dict[str, frozenset[str]] = {
    "coordinator": frozenset({REASONING, STRUCTURED_DECISIONS}),
    "coding": frozenset({CODING, WORKSPACE_EDITING}),
}

# Authentication categories (what kind of credential — never the credential).
AUTH_SUBSCRIPTION = "subscription"   # a consumer subscription login managed by a CLI
AUTH_API = "api"                     # a provider API key, billed separately
AUTH_CLOUD = "cloud"                 # cloud-provider credentials (AWS / Google Cloud)
AUTH_LOCAL = "local"                 # a local endpoint, no external auth
AUTH_NONE = "none"                   # disabled

# Billing models (who pays) — used verbatim in guided setup so we never imply, e.g., that a
# consumer subscription includes unrelated API credits.
BILL_SUBSCRIPTION = "subscription allowance"
BILL_API = "separate API billing"
BILL_CLOUD = "cloud-provider billing"
BILL_LOCAL = "local compute"
BILL_NONE = "none"


#: Per-provider capability + auth + billing declarations, keyed by provider key. This is the
#: single source of truth the catalogue and selection both read.
_CAP: dict[str, dict] = {
    # -- coordinators -------------------------------------------------------------------------
    "claude_cli": {
        "caps": {REASONING, STRUCTURED_DECISIONS, STREAMING, USAGE_REPORTING, SUBSCRIPTION_AUTH,
                 SESSION_RESUME},
        "auth": AUTH_SUBSCRIPTION, "bill": BILL_SUBSCRIPTION,
    },
    "anthropic": {  # anthropic_api
        "caps": {REASONING, STRUCTURED_DECISIONS, TOOL_CALLING, USAGE_REPORTING, API_AUTH},
        "auth": AUTH_API, "bill": BILL_API,
    },
    "anthropic_bedrock": {
        "caps": {REASONING, STRUCTURED_DECISIONS, TOOL_CALLING, USAGE_REPORTING, CLOUD_AUTH},
        "auth": AUTH_CLOUD, "bill": BILL_CLOUD,
    },
    "anthropic_vertex": {
        "caps": {REASONING, STRUCTURED_DECISIONS, TOOL_CALLING, USAGE_REPORTING, CLOUD_AUTH},
        "auth": AUTH_CLOUD, "bill": BILL_CLOUD,
    },
    "openai_api": {
        "caps": {REASONING, STRUCTURED_DECISIONS, TOOL_CALLING, USAGE_REPORTING, API_AUTH},
        "auth": AUTH_API, "bill": BILL_API,
    },
    "openai_compatible": {
        "caps": {REASONING, STRUCTURED_DECISIONS, USAGE_REPORTING, API_AUTH},
        "auth": AUTH_API, "bill": BILL_API,
    },
    "openai_compatible_local": {
        "caps": {REASONING, STRUCTURED_DECISIONS, LOCAL_EXECUTION},
        "auth": AUTH_LOCAL, "bill": BILL_LOCAL,
    },
    # beta.5: local browser-session gateway (CatGPT-Gateway). Coordinator-only reasoning; the
    # subscription auth lives in the gateway's own browser session, so Aithernet-side auth is LOCAL
    # (no cloud credential) and billing is the user's existing subscription (local compute path).
    "catgpt_gateway": {
        "caps": {REASONING, STRUCTURED_DECISIONS, LOCAL_EXECUTION},
        "auth": AUTH_LOCAL, "bill": BILL_LOCAL,
    },
    "gemini_api": {
        "caps": {REASONING, STRUCTURED_DECISIONS, USAGE_REPORTING, API_AUTH},
        "auth": AUTH_API, "bill": BILL_API,
    },
    "gemini_vertex": {
        "caps": {REASONING, STRUCTURED_DECISIONS, USAGE_REPORTING, CLOUD_AUTH},
        "auth": AUTH_CLOUD, "bill": BILL_CLOUD,
    },
    "gemini_cli": {
        "caps": {REASONING, STRUCTURED_DECISIONS, CLOUD_AUTH},
        "auth": AUTH_CLOUD, "bill": BILL_CLOUD,
    },
    # -- coding -------------------------------------------------------------------------------
    "codex_cli": {
        "caps": {CODING, WORKSPACE_EDITING, COMMAND_EXECUTION, TOOL_CALLING, STREAMING,
                 SUBSCRIPTION_AUTH, SESSION_RESUME},
        "auth": AUTH_SUBSCRIPTION, "bill": BILL_SUBSCRIPTION,
    },
    "codex_api": {
        "caps": {CODING, WORKSPACE_EDITING, COMMAND_EXECUTION, TOOL_CALLING, API_AUTH},
        "auth": AUTH_API, "bill": BILL_API,
    },
    "claude_code": {  # claude_code_cli
        "caps": {CODING, WORKSPACE_EDITING, COMMAND_EXECUTION, TOOL_CALLING, STREAMING,
                 SUBSCRIPTION_AUTH, SESSION_RESUME},
        "auth": AUTH_SUBSCRIPTION, "bill": BILL_SUBSCRIPTION,
    },
    "local_coding": {
        "caps": {CODING, WORKSPACE_EDITING, LOCAL_EXECUTION},
        "auth": AUTH_LOCAL, "bill": BILL_LOCAL,
    },
    # -- disabled -----------------------------------------------------------------------------
    "disabled": {"caps": set(), "auth": AUTH_NONE, "bill": BILL_NONE},
}


def capabilities_for(provider_key: str) -> frozenset[str]:
    """The declared capability set for a provider key (empty for unknown/disabled)."""
    return frozenset(_CAP.get(provider_key, {}).get("caps", set()))


def auth_category(provider_key: str) -> str:
    """The authentication category (subscription/api/cloud/local/none) for a provider key."""
    return _CAP.get(provider_key, {}).get("auth", AUTH_NONE)


def billing_model(provider_key: str) -> str:
    """The billing model string for a provider key (used verbatim in guided setup)."""
    return _CAP.get(provider_key, {}).get("bill", BILL_NONE)


def satisfies_role(provider_key: str, role: str) -> bool:
    """True iff the provider's capabilities satisfy the role requirement.

    ``disabled`` never satisfies a role. Coding is NOT inferred from text/reasoning — a provider
    must explicitly declare ``coding`` + ``workspace_editing`` to be eligible for the coding role.
    """
    if provider_key == "disabled":
        return False
    required = ROLE_REQUIREMENTS.get(role)
    if required is None:
        return False
    return required <= capabilities_for(provider_key)


def eligible_providers(role: str, candidate_keys) -> list[str]:
    """Filter ``candidate_keys`` to those whose capabilities satisfy ``role`` (order preserved)."""
    return [k for k in candidate_keys if satisfies_role(k, role)]


def has_capability(provider_key: str, capability: str) -> bool:
    """True iff the provider declares ``capability``."""
    return capability in capabilities_for(provider_key)
