"""Coordinator + coding provider catalogue, detection, readiness, and protected config store.

Two runtime identity models are first-class (PHASE 6):

* **workstation** — provider authentication belongs to the desktop Linux user; CLI providers
  (Gemini CLI, Claude Code) inherit that user's login. Aithernet runs under the same identity.
* **appliance** — headless: providers use explicitly configured, service-compatible credentials
  referenced by an environment-variable NAME (never a desktop browser session/keychain).

The SINGLE source of truth is the canonical ``NodeConfig`` YAML (the ``coordinator`` and
``coding_agent`` sections) that the coordinator + coding runtimes load via
:func:`aithernet.config.load_config` — so the selected provider is the executed provider.
Credentials are written only as ``env:VAR_NAME`` references (resolved by the loader), never values,
never in
the project tree, packages, or telemetry. A legacy ``agents.json`` is migrated into NodeConfig once
and never used as a second source of truth. ``status``/``test`` report a provider authenticated only
when a bounded, non-sensitive readiness request against the actual runtime config succeeds — never
merely because an executable exists.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

COORDINATOR = "coordinator"
CODING = "coding"
ROLES = (COORDINATOR, CODING)

WORKSTATION = "workstation"
APPLIANCE = "appliance"
IDENTITY_MODELS = (WORKSTATION, APPLIANCE)

DISABLED = "disabled"

#: Identity-probe timeout (seconds) for executable/version detection.
_PROBE_TIMEOUT = 8.0

# Truthful provider support classification (no "dev-only"; Aithernet is BYO/provider-agnostic).
REAL_TESTED = "fully-supported-real-tested"        # implemented + passed a real acceptance test
AUTO_TESTED = "implemented-auto-tested"     # real adapter + automated tests (mocked external)
CONFIGURABLE = "configurable-not-externally-tested"
UNSUPPORTED = "unsupported"
SUPPORT_LEVELS = (REAL_TESTED, AUTO_TESTED, CONFIGURABLE, UNSUPPORTED)


@dataclass(frozen=True)
class ProviderInfo:
    key: str
    role: str
    kind: str               # cli | api | local | disabled
    display: str
    executable: str | None  # official executable for cli providers
    auth: str               # human-readable auth requirement
    support_level: str      # one of SUPPORT_LEVELS — the truthful status of this adapter
    preferred_beta: bool = False   # the preferred default for the beta pair (still BYO-selectable)
    notes: str = ""

    # -- beta.4 capability metadata (delegated to aithernet.agents.capabilities, keyed by `key`) --
    def capabilities(self) -> frozenset[str]:
        from aithernet.agents import capabilities as caps
        return caps.capabilities_for(self.key)

    def auth_category(self) -> str:
        from aithernet.agents import capabilities as caps
        return caps.auth_category(self.key)

    def billing(self) -> str:
        from aithernet.agents import capabilities as caps
        return caps.billing_model(self.key)

    def satisfies_role(self) -> bool:
        from aithernet.agents import capabilities as caps
        return caps.satisfies_role(self.key, self.role)


# Aithernet is BYO-agent / provider-agnostic: the coordinator + coding agents are selectable
# implementations behind canonical interfaces. The preferred beta DEFAULTS are Gemini CLI
# (coordinator) and Codex CLI (coding) — both still user-selectable, not hardcoded. Every adapter
# below is genuinely implemented; each is classified by its TRUTHFUL status. None are yet
# REAL_TESTED (a real-provider acceptance test runs at the operator gate); they become REAL_TESTED
# only after that passes.
CATALOGUE: tuple[ProviderInfo, ...] = (
    # -- coordinators -------------------------------------------------------------------------
    ProviderInfo("claude_cli", COORDINATOR, "cli", "Claude — signed in through Claude.ai Pro/Max",
                 "claude",
                 "Claude run in bounded non-interactive mode via the official `claude` executable; "
                 "uses your Claude.ai subscription login (subscription allowance).",
                 support_level=AUTO_TESTED),
    ProviderInfo("anthropic", COORDINATOR, "api", "Anthropic API", None,
                 "Anthropic API key via an env-var reference (e.g. ANTHROPIC_API_KEY). Billed "
                 "separately by your Anthropic API account — distinct from a Claude subscription.",
                 support_level=AUTO_TESTED),
    ProviderInfo("anthropic_bedrock", COORDINATOR, "api", "Anthropic through AWS Bedrock", None,
                 "Claude on AWS Bedrock using documented AWS credentials. Cloud-provider billing.",
                 support_level=CONFIGURABLE,
                 notes="Bedrock adapter; live qualification pending without AWS credentials"),
    ProviderInfo("anthropic_vertex", COORDINATOR, "api", "Anthropic through Google Vertex AI", None,
                 "Claude on Google Vertex AI using documented Google Cloud credentials. "
                 "Cloud-provider billing.",
                 support_level=CONFIGURABLE,
                 notes="Vertex Claude adapter; live qualification pending without GCP creds"),
    ProviderInfo("openai_api", COORDINATOR, "api", "OpenAI API", None,
                 "OpenAI API key via an env-var reference (e.g. OPENAI_API_KEY). Separate API "
                 "billing.",
                 support_level=AUTO_TESTED),
    ProviderInfo("openai_compatible", COORDINATOR, "api", "OpenAI-compatible hosted endpoint", None,
                 "Any OpenAI-compatible endpoint (base URL + API key). Separate API billing.",
                 support_level=AUTO_TESTED),
    ProviderInfo("openai_compatible_local", COORDINATOR, "local",
                 "Local OpenAI-compatible endpoint", None,
                 "Local/self-hosted OpenAI-compatible server (e.g. http://127.0.0.1:11434/v1); API "
                 "key optional. Local compute — no external billing.",
                 support_level=CONFIGURABLE,
                 notes="openai_compatible adapter against a local endpoint"),
    ProviderInfo("catgpt_gateway", COORDINATOR, "local",
                 "CatGPT Gateway — local OpenAI-compatible browser gateway", None,
                 "A local browser-session gateway (CatGPT-Gateway) that re-exposes your ChatGPT/"
                 "Claude web subscription as an OpenAI-compatible endpoint (default "
                 "http://127.0.0.1:8000/v1). Coordinator reasoning only — keep the coding agent "
                 "(e.g. codex_cli) configured separately. Start the gateway yourself; Aithernet "
                 "never needs its browser cookies. Local compute — your subscription pays.",
                 support_level=CONFIGURABLE,
                 notes="OpenAI-compatible adapter against a local subscription-backed gateway; "
                       "coordinator-only, api key optional (local)"),
    ProviderInfo("gemini_api", COORDINATOR, "api", "Gemini API", None,
                 "Google Generative Language API key via an env-var reference (e.g. "
                 "GEMINI_API_KEY). Separate API billing; distinct from the Gemini CLI.",
                 support_level=AUTO_TESTED, preferred_beta=True),
    ProviderInfo("gemini_vertex", COORDINATOR, "api", "Gemini through Vertex AI", None,
                 "Gemini on Google Vertex AI using documented Google Cloud credentials "
                 "(project + location). Cloud-provider billing.",
                 support_level=CONFIGURABLE,
                 notes="Vertex Gemini adapter; live qualification pending without GCP creds"),
    ProviderInfo("gemini_cli", COORDINATOR, "cli", "Installed Google CLI (Gemini)", "gemini",
                 "The installed official Google CLI (`gemini`), using its current Google Cloud "
                 "authentication that belongs to the runtime user. Google AI Pro alone does not "
                 "guarantee CLI access.",
                 support_level=AUTO_TESTED),
    ProviderInfo(DISABLED, COORDINATOR, "disabled", "Disabled", None,
                 "No coordinator; missions run without AI planning.", support_level=AUTO_TESTED),
    # -- coding -------------------------------------------------------------------------------
    ProviderInfo("codex_cli", CODING, "cli", "Codex — signed in through ChatGPT", "codex",
                 "Codex CLI (`codex`) using your ChatGPT subscription sign-in (subscription "
                 "allowance). Auth belongs to the runtime identity.",
                 support_level=AUTO_TESTED, preferred_beta=True),
    ProviderInfo("codex_api", CODING, "api", "Codex / OpenAI API coding profile", None,
                 "Codex CLI driven by an OpenAI API key (separate API billing) where supported, "
                 "instead of a ChatGPT subscription.",
                 support_level=CONFIGURABLE,
                 notes="API-key Codex profile; live qualification pending without an OpenAI key"),
    ProviderInfo("claude_code", CODING, "cli", "Claude Code — signed in through Claude.ai Pro/Max",
                 "claude",
                 "Claude Code CLI (`claude`) using your Claude.ai subscription login (subscription "
                 "allowance). Auth belongs to the runtime user.",
                 support_level=AUTO_TESTED),
    ProviderInfo("local_coding", CODING, "local", "Local coding provider", None,
                 "A local coding agent / OpenAI-compatible local endpoint. Local compute — no "
                 "external billing.",
                 support_level=CONFIGURABLE,
                 notes="local coding profile; endpoint-neutral"),
    ProviderInfo(DISABLED, CODING, "disabled", "Disabled", None,
                 "No coding agent.", support_level=AUTO_TESTED),
)


def providers_for(role: str) -> list[ProviderInfo]:
    return [p for p in CATALOGUE if p.role == role]


def get_info(role: str, key: str) -> ProviderInfo | None:
    for p in CATALOGUE:
        if p.role == role and p.key == key:
            return p
    return None


# ---------------------------------------------------------------------------
# Source of truth = the canonical NodeConfig YAML (coordinator / coding_agent
# sections). The coordinator + coding runtimes load the SAME file via
# aithernet.config.load_config, so the selected provider is the executed provider.
# Secrets are written only as `env:VAR_NAME` references (resolved by the loader),
# never as values. ``agents.json`` is a deprecated compatibility input — it is
# migrated into NodeConfig once and never used as a second source of truth.
# ---------------------------------------------------------------------------
#: NodeConfig section name per role.
_SECTION = {COORDINATOR: "coordinator", CODING: "coding_agent"}


def _state_root(state_root: Path | None = None) -> Path:
    if state_root is not None:
        return Path(state_root)
    return Path(os.environ.get("AITHERNET_STATE_ROOT",
                               str(Path.home() / ".local/share/aithernet")))


def node_config_path(state_root: Path | None = None) -> Path:
    """The node config file the runtime loads. Matches aithernet.config.load_config resolution:
    AITHERNET_CONFIG if set, else <state_root>/config/node.yaml (the path the node service pins)."""
    env = os.environ.get("AITHERNET_CONFIG")
    if env:
        return Path(env)
    return _state_root(state_root) / "config" / "node.yaml"


def _read_node_yaml(state_root: Path | None = None) -> dict:
    import yaml
    path = node_config_path(state_root)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _write_node_yaml(data: dict, state_root: Path | None = None) -> Path:
    import yaml
    path = node_config_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".yaml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.safe_dump(data, fh, sort_keys=True, default_flow_style=False)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Secure credential store: the 0600 env file the systemd USER unit loads
# (EnvironmentFile=-%h/.config/aithernet/aithernet.env). API keys live ONLY here as
# ``NAME=value`` lines; node.yaml records only the ``env:NAME`` reference. Values are never
# returned, logged, or echoed.
# ---------------------------------------------------------------------------
def secret_env_file() -> Path:
    """The 0600 credential env file the systemd user unit reads (matches the unit's %h/.config)."""
    return Path.home() / ".config" / "aithernet" / "aithernet.env"


_VALID_NAME = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")


def set_secret(name: str, value: str, *, env_file: Path | None = None) -> Path:
    """Store ``NAME=value`` in the secured env file (0600), replacing any prior value. Supports
    rotation (call again). The value is never returned or logged."""
    if not _VALID_NAME.match(name):
        raise ValueError("secret name must be an UPPER_SNAKE_CASE environment-variable name")
    if not value:
        raise ValueError("secret value must not be empty")
    path = env_file or secret_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    existing = []
    if path.is_file():
        existing = [ln for ln in path.read_text().splitlines()
                    if ln.strip() and not ln.startswith(f"{name}=")]
    lines = [*existing, f"{name}={value}"]
    tmp = path.with_suffix(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def remove_secret(name: str, *, env_file: Path | None = None) -> bool:
    """Remove ``NAME`` from the env file. Returns True if it was present."""
    path = env_file or secret_env_file()
    if not path.is_file():
        return False
    lines = path.read_text().splitlines()
    kept = [ln for ln in lines if ln.strip() and not ln.startswith(f"{name}=")]
    if len(kept) == len(lines):
        return False
    fd = os.open(path.with_suffix(".env.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(("\n".join(kept) + "\n") if kept else "")
    os.replace(path.with_suffix(".env.tmp"), path)
    os.chmod(path, 0o600)
    return True


def list_secret_names(env_file: Path | None = None) -> list[str]:
    """The NAMES present in the env file (never the values)."""
    path = env_file or secret_env_file()
    if not path.is_file():
        return []
    return sorted({ln.split("=", 1)[0] for ln in path.read_text().splitlines()
                   if "=" in ln and ln.strip()})


def _readiness_path(state_root: Path | None = None) -> Path:
    # a GENERATED cache of the last bounded-test outcome (status only). Never a selection source.
    return node_config_path(state_root).parent / "agents-readiness.json"


def _load_readiness(state_root: Path | None = None) -> dict:
    p = _readiness_path(state_root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _section(role: str, state_root: Path | None = None) -> dict:
    """The raw NodeConfig section for ``role`` (for display — never the resolved secret value)."""
    return dict(_read_node_yaml(state_root).get(_SECTION[role], {}) or {})


def _api_key_ref(section: dict) -> str:
    """The env-var NAME from an `api_key: env:NAME` reference (never a value)."""
    raw = str(section.get("api_key", "") or "")
    return raw[len("env:"):] if raw.startswith("env:") else ""


def resolve_secret_value(ref: str, *, env_file: Path | None = None) -> str | None:
    """Resolve a managed secret VALUE by its env-var NAME — from the process environment OR the
    managed 0600 store — so a CLI process (which lacks the systemd service's ``EnvironmentFile``)
    can send the SAME credential the running service would. Returns None if unavailable.

    The config loader only resolves ``env:NAME`` refs from ``os.environ``; a value stored with
    ``aithernet agents set-secret`` lives in the 0600 file, so a bare CLI ``agents test --live``
    would otherwise send no Authorization header. This closes that gap. The value is NEVER logged.
    """
    if not ref:
        return None
    val = os.environ.get(ref)
    if val:
        return val
    path = env_file or secret_env_file()
    if path.is_file():
        try:
            for ln in path.read_text().splitlines():
                if ln.startswith(f"{ref}="):
                    return ln.split("=", 1)[1]
        except OSError:
            return None
    return None


def _credential_available(ref: str) -> bool:
    """Whether the credential NAME is actually available to the runtime (never the value).

    True if it is in this process's environment, stored in the managed 0600 credential file, or
    seen by the running managed service. This makes ``api_key_present`` truthful regardless of
    which shell runs the status check (the beta.9 defect: it only checked the CLI's own env).
    """
    if not ref:
        return False
    if os.environ.get(ref):
        return True
    if ref in list_secret_names():
        return True
    try:
        from aithernet.provisioning import services as _services
        return _services.service_sees_env(ref) is True
    except Exception:  # noqa: BLE001 — status must never crash on a service-probe error
        return False


# ---------------------------------------------------------------------------
# Detection (executable + version) — bounded, no paid inference
# ---------------------------------------------------------------------------
@dataclass
class Probe:
    executable: str | None
    found: bool
    version: str | None
    detail: str = ""


def detect_executable(exe: str | None) -> Probe:
    if not exe:
        return Probe(None, False, None, "no executable configured")
    resolved = shutil.which(exe)
    if resolved is None:
        return Probe(exe, False, None, f"'{exe}' not found on PATH")
    version = None
    try:
        proc = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT)
        out = f"{proc.stdout}\n{proc.stderr}"
        import re
        m = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.]+)?\b", out)
        version = m.group(0) if m else None
    except (OSError, subprocess.TimeoutExpired):
        return Probe(resolved, True, None, "executable present but --version failed")
    return Probe(resolved, True, version, "ok")


# ---------------------------------------------------------------------------
# Load the EXACT NodeConfig the runtime uses (so selection == execution)
# ---------------------------------------------------------------------------
def _load_node_config(state_root: Path | None = None):
    """Load NodeConfig from the same path the coordinator/coding runtimes load."""
    from aithernet.config import load_config
    return load_config(node_config_path(state_root))


def capacity_pool_inputs(state_root: Path | None = None) -> dict[str, dict]:
    """Build the (non-secret) per-role inputs for :func:`aithernet.agents.capacity.build_pools`.

    Reads the RAW node.yaml sections so ``api_key`` is the env-var REFERENCE (e.g.
    ``env:ANTHROPIC_API_KEY``) — never a resolved value — and adds region/project/base_url. This is
    what makes the capacity-pool id non-secret.
    """
    data = _read_node_yaml(state_root)
    out: dict[str, dict] = {}
    for role, section_name in _SECTION.items():
        section = data.get(section_name, {}) or {}
        provider = section.get("provider") or ("openai_compatible" if role == COORDINATOR
                                               else "claude_code")
        out[role] = {
            "provider": provider,
            "api_key_ref": str(section.get("api_key") or ""),
            "base_url": str(section.get("base_url") or ""),
            "region": str(section.get("region") or ""),
            "project": str(section.get("project") or ""),
            "concurrency_limit": int(section.get("concurrency_limit", 1) or 1),
        }
    return out


def fallback_chain(role: str, state_root: Path | None = None) -> list[str]:
    """The explicit, role-valid fallback chain for ``role`` from node.yaml (primary + fallbacks)."""
    from aithernet.agents import fallback as fb
    data = _read_node_yaml(state_root)
    section = data.get(_SECTION[role], {}) or {}
    primary = section.get("provider") or ""
    fallbacks = section.get("fallback_providers") or []
    return fb.resolve_chain(role, primary, list(fallbacks))


def set_fallback_chain(role: str, providers: list[str], state_root: Path | None = None) -> dict:
    """Persist an explicit fallback chain for ``role`` (each entry must satisfy the role)."""
    from aithernet.agents import capabilities as caps
    from aithernet.agents import fallback as fb
    # Each entry may be `provider` or `provider:profile`; the provider key must satisfy the role.
    invalid = [p for p in providers if not caps.satisfies_role(fb.split_entry(p)[0], role)]
    if invalid:
        raise ValueError(f"providers not eligible for role {role}: {', '.join(invalid)}")
    data = _read_node_yaml(state_root)
    section = data.setdefault(_SECTION[role], {})
    section["fallback_providers"] = list(providers)
    _write_node_yaml(data, state_root)
    return {"role": role, "fallback_providers": list(providers)}


# ---------------------------------------------------------------------------
# Status + bounded readiness test
# ---------------------------------------------------------------------------
def runtime_identity(state_root: Path | None = None) -> dict:
    """The Linux runtime identity the node runs under. The identity MODEL (workstation/appliance)
    is an install-time choice recorded in the guided-setup state, not provider config."""
    model = WORKSTATION
    try:
        from aithernet.provisioning import state as sstate
        st = sstate.load_state(_state_root(state_root))
        if st and st.identity_model:
            model = st.identity_model
    except Exception:  # noqa: BLE001
        pass
    return {"linux_user": getpass.getuser(), "identity_model": model}


def _role_provider_config(role: str, state_root: Path | None = None):
    """The canonical typed config object for ``role`` from NodeConfig (what the runtime builds)."""
    nc = _load_node_config(state_root)
    return nc.coordinator if role == COORDINATOR else nc.coding_agent


def _selected_provider(role: str, state_root: Path | None = None) -> str:
    """The provider the operator/setup EXPLICITLY selected in node.yaml. An absent section means
    'unconfigured' (DISABLED) — distinct from the NodeConfig model default, so a fresh node reads as
    unconfigured rather than the built-in fallback."""
    return (_section(role, state_root).get("provider") or DISABLED)


def status(role: str, *, state_root: Path | None = None) -> dict:
    """Sanitised provider status from the canonical NodeConfig. Never shows token values/paths."""
    section = _section(role, state_root)          # raw YAML (for the api-key REF name, not value)
    cfg = _role_provider_config(role, state_root)  # the typed config the runtime would build
    provider = _selected_provider(role, state_root)  # explicit selection (absent => unconfigured)
    info = get_info(role, provider)
    last = _load_readiness(state_root).get(role, {})
    out: dict = {
        "role": role,
        "provider": provider,
        "display": info.display if info else provider,
        "support_level": info.support_level if info else UNSUPPORTED,
        "preferred_beta": bool(info.preferred_beta) if info else False,
        "configured": provider not in ("", DISABLED),
        "model": getattr(cfg, "model", None) or None,
        "endpoint": getattr(cfg, "base_url", None) or None,
        "runtime_identity": runtime_identity(state_root),
        "last_test": {"status": last.get("status", "never"), "at": last.get("at"),
                      "live": bool(last.get("live"))},
        "config_source": str(node_config_path(state_root)),
    }
    # End-to-end auth is "verified" ONLY after a live test actually completed — a non-live config
    # probe never grants live readiness (so doctor can't over-claim).
    live_verified = last.get("status") == "ready" and bool(last.get("live"))
    selected = provider not in ("", DISABLED)
    readiness: dict = {
        "supported": info is not None,
        "selected": selected,
        "configured": selected and info is not None,
        "live_test": last.get("status", "never"),
        "live_verified": live_verified,
        "mission_ready": live_verified or (selected and provider == DISABLED),
    }
    if info and info.kind == "cli":
        probe = detect_executable(getattr(cfg, "executable", None) or (info.executable or ""))
        out["executable"] = probe.executable
        out["executable_found"] = probe.found
        out["executable_version"] = probe.version
        readiness["executable_present"] = probe.found
        out["auth_readiness"] = "live-verified" if live_verified else (
            "executable-present-auth-unverified" if probe.found else "executable-missing")
    elif info and info.kind in ("api", "local"):
        ref = _api_key_ref(section)
        out["api_key_ref"] = ref or None
        present = bool(ref) and _credential_available(ref)
        out["api_key_present"] = present
        readiness["credential_reference"] = bool(ref)
        readiness["credential_available"] = present
        out["auth_readiness"] = "live-verified" if live_verified else (
            "key-reference-set" if ref else "no-key-reference")
    else:
        out["auth_readiness"] = "n/a"
    # beta.5: CatGPT-Gateway is a local gateway — report its endpoint reachability (a bounded,
    # tokenless TCP probe) and a local-specific auth-readiness label. "configured" here means the
    # local gateway is selected; "live-verified" still requires an actual live test to have passed.
    if provider == "catgpt_gateway":
        endpoint = out.get("endpoint") or catgpt_gateway_default_base_url()
        out["endpoint"] = endpoint
        readiness["endpoint_reachable"] = _endpoint_reachable(endpoint)
        out["auth_readiness"] = "live-verified" if live_verified else "local-gateway-configured"
        # beta.5 Part D: distinguish an Aithernet-managed gateway (aithernet catgpt) from a manually
        # run external endpoint, and surface the coarse gateway lifecycle state when managed.
        # beta.7 (FIX 2): expose configured vs discovered vs effective model, credential/endpoint
        # state, and whether an auto-repair (persist the discovered model) is available — and NEVER
        # claim mission_ready unless an effective model is configured for the live-test path.
        configured_model = getattr(cfg, "model", None) or None
        managed, gw_status, gw_model = _catgpt_managed_status(state_root)
        out["managed_gateway"] = managed
        out["configured_model"] = configured_model
        out["discovered_gateway_model"] = gw_model
        out["effective_model"] = configured_model or gw_model
        out["credential_available"] = out.get("api_key_present", False)
        if managed:
            out["gateway_status"] = gw_status
            readiness["gateway_status"] = gw_status
        if configured_model:
            out["model"] = configured_model
        elif gw_model:
            out["model"] = gw_model
        repair_available = bool(managed and gw_status == "ready"
                                and not configured_model and gw_model)
        out["repair_available"] = repair_available
        out["repair_action"] = (
            "aithernet catgpt status   (auto-persists the discovered model)"
            if repair_available else None)
        readiness["model_configured"] = bool(configured_model)
        readiness["repair_available"] = repair_available
        if not configured_model:
            # live-test needs a configured model; do not over-claim readiness without one
            readiness["mission_ready"] = False
    out["readiness"] = readiness
    return out


def test(role: str, *, live: bool = False, state_root: Path | None = None) -> dict:
    """Run a BOUNDED readiness request against the provider built from the SAME NodeConfig the
    runtime uses (selection == execution). With ``live`` (coordinator) it makes one minimal real
    inference; otherwise it checks config/executable readiness without spending tokens."""
    cfg = _role_provider_config(role, state_root)
    provider_name = _selected_provider(role, state_root)
    # beta.5: hydrate a managed-secret credential so a CLI live test sends the same Authorization
    # the running service would. The loader only resolves `env:NAME` from os.environ; a `set-secret`
    # value lives in the 0600 store, so without this the CLI probe would send no bearer token (the
    # exact CatGPT-Gateway "not reachable" false negative). Never logs the value.
    if not getattr(cfg, "api_key", None):
        _ref = _api_key_ref(_section(role, state_root))
        _val = resolve_secret_value(_ref)
        if _val:
            cfg = cfg.model_copy(update={"api_key": _val})
    info = get_info(role, provider_name)
    # `live` reflects whether a REAL provider request actually completed — set True only after one.
    result: dict = {"role": role, "provider": provider_name, "live": False,
                    "config_source": str(node_config_path(state_root))}
    if provider_name in ("", DISABLED):
        result.update(ready=True, status="disabled", detail="provider disabled")
        return _persist_test(role, "ready", state_root, result)
    if info is None:
        result.update(ready=False, status="unknown_provider",
                      detail=f"'{provider_name}' is not a known {role} provider")
        return _persist_test(role, "error", state_root, result)
    try:
        if role == COORDINATOR:
            from aithernet.coordinator.providers import build_provider
        else:
            from aithernet.coding_agent.providers import build_provider
        provider = build_provider(cfg)        # the EXACT object the runtime constructs
        if provider is None:
            raise RuntimeError(f"no {role} provider implementation for '{provider_name}'")
        result["executed_provider"] = getattr(provider, "name", "?")
        missing = provider.check_ready()
        if missing and provider_name == "catgpt_gateway" and "model" in missing:
            # beta.7 (FIX 1): before failing with missing:[model], auto-persist the gateway's
            # discovered model (when exactly one) so a fresh client's live test just works.
            repair = ensure_catgpt_coordinator_model(state_root)
            if repair.get("persisted"):
                cfg = _role_provider_config(role, state_root)
                if not getattr(cfg, "api_key", None):
                    _val2 = resolve_secret_value(_api_key_ref(_section(role, state_root)))
                    if _val2:
                        cfg = cfg.model_copy(update={"api_key": _val2})
                provider = build_provider(cfg)
                result["executed_provider"] = getattr(provider, "name", "?")
                result["auto_persisted_model"] = repair.get("model")
                missing = provider.check_ready()
        if missing:
            detail = "configuration/auth not ready (bounded executable/config probe)"
            # beta.5: for CatGPT-Gateway, an unset model gets a precise, non-fabricating message
            # (discovery-available => choose one; discovery-unavailable => say so) instead of a
            # generic "not ready" — the model is never defaulted to a value the gateway may reject.
            if provider_name == "catgpt_gateway" and "model" in missing:
                detail = _catgpt_model_missing_detail(provider)
            result.update(ready=False, status="not_ready", missing=missing, detail=detail)
            return _persist_test(role, "not_ready", state_root, result)
        if not live:
            result.update(ready=True, status="ready",
                          detail="ready (bounded executable/config probe; pass --live to verify "
                                 "the provider end-to-end)")
            return _persist_test(role, "ready", state_root, result)
        # --live: a REAL, bounded provider request must complete and validate.
        if role == COORDINATOR:
            import asyncio
            import time as _t
            t0 = _t.monotonic()
            # beta.5: CatGPT-Gateway uses a classified live probe so a reachable-but-failing gateway
            # is reported honestly (credential_rejected / unsupported_gateway_feature /
            # structured_output_incompatible / browser_session_not_ready / …), never "unreachable".
            if provider_name == "catgpt_gateway":
                probe = asyncio.run(provider.live_probe())
                latency = round((_t.monotonic() - t0) * 1000, 1)
                if probe.get("ok"):
                    result.update(
                        ready=True, status="ready", live=True, latency_ms=latency,
                        model=getattr(cfg, "model", None), endpoint=getattr(cfg, "base_url", None),
                        endpoint_reachable=True, probe=_sanitise_probe(probe),
                        detail="live bounded inference succeeded")
                    return _persist_test(role, "ready", state_root, result)
                result.update(
                    ready=False, status=probe.get("category", "error"), live=False,
                    category=probe.get("category"), latency_ms=latency,
                    endpoint=getattr(cfg, "base_url", None),
                    endpoint_reachable=probe.get("endpoint_reachable", False),
                    detail=probe.get("detail", "live probe failed"))
                return _persist_test(role, "error", state_root, result)
            meta = asyncio.run(provider.healthcheck())
            result.update(ready=True, status="ready", live=True, probe=_sanitise_probe(meta),
                          latency_ms=round((_t.monotonic() - t0) * 1000, 1),
                          detail="live bounded inference succeeded")
            return _persist_test(role, "ready", state_root, result)
        # CODING: one real, bounded, non-destructive Codex invocation in an isolated workspace
        result.update(_live_coding_probe(provider, cfg))
        return _persist_test(role, "ready" if result.get("ready") else "error",
                             state_root, result)
    except Exception as exc:  # noqa: BLE001 — surface a sanitised category, never raw secrets
        # NB: CatGPT-Gateway live errors are classified inside live_probe() (reachable-vs-not,
        # credential, compatibility, structured-output, session) and never reach here as a blanket
        # "unreachable". This path is a last resort for an unexpected error; stay honest + generic.
        result.update(ready=False, status="error", live=False, detail=f"{type(exc).__name__}")
        return _persist_test(role, "error", state_root, result)


def _sanitise_probe(meta: dict) -> dict:
    """Keep only safe, non-private fields from a healthcheck result."""
    return {k: meta[k] for k in ("model", "models", "latency_ms", "ok", "provider") if k in meta}


def _safe_ws(path: str) -> str:
    """Represent a workspace path without exposing the home directory."""
    return str(path).replace(str(Path.home()), "~")


def _live_coding_probe(provider, cfg) -> dict:
    """ONE real, bounded, non-destructive coding-provider request in an ISOLATED temp workspace.

    Embeds a unique nonce the provider must echo back, so success cannot be reported without an
    actual provider response (proving the selected adapter executed). It creates no repo
    changes; the
    temp workspace is removed afterwards. Returns sanitised metadata only — no credentials/paths.
    """
    import asyncio
    import datetime as _dt
    import secrets
    import tempfile
    import time as _t

    from aithernet.coding_agent.contracts import CodingTaskExecutionInput

    nonce = secrets.token_hex(8)
    expected = {"provider_check": "codex_cli", "nonce": nonce, "result": "ready"}
    workspace = tempfile.mkdtemp(prefix="aithernet_codex_readiness_")
    objective = (
        "Readiness check only. Do NOT create, modify, or delete any files and do NOT call any "
        "tools. Reply with EXACTLY this one-line JSON object and nothing else: "
        + json.dumps(expected, separators=(",", ":")))
    task = CodingTaskExecutionInput(
        task_id=f"readiness-{nonce}", objective=objective, workspace=workspace,
        current_time=_dt.datetime.now(_dt.UTC),
        expected_outputs=["a single JSON object echoing the provided nonce"],
        reporting_requirements=["return only the exact JSON object"])
    base: dict = {"live": True, "real_request": True,
                  "executed_provider": getattr(provider, "name", "?"),
                  "model": getattr(cfg, "model", None) or None,
                  "workspace": _safe_ws(workspace)}
    try:
        client_version = provider.identity_version()
    except Exception:  # noqa: BLE001
        client_version = None
    base["client_version"] = client_version
    t0 = _t.monotonic()
    try:
        res = asyncio.run(provider.execute(task))
    except Exception as exc:  # noqa: BLE001 — sanitised category only
        with contextlib.suppress(OSError):
            shutil.rmtree(workspace)
        base.update(ready=False, status="error",
                    detail=f"live Codex invocation failed: {type(exc).__name__}",
                    workspace_cleaned=not Path(workspace).exists())
        return base
    latency_ms = round((_t.monotonic() - t0) * 1000, 1)
    response = f"{res.summary}\n{res.stdout}"
    validated = (res.status == "completed") and (nonce in response)
    leftover = [p.relative_to(workspace).as_posix()
                for p in sorted(Path(workspace).rglob("*")) if p.is_file()]
    with contextlib.suppress(OSError):
        shutil.rmtree(workspace)
    base.update(
        ready=bool(validated),
        status="ready" if validated else "not_ready",
        detail=("live bounded Codex invocation succeeded" if validated else
                "live Codex invocation did not return the validated nonce response"),
        response_status=res.status,
        nonce_validated=bool(nonce in response),
        latency_ms=latency_ms,
        workspace_files_left=leftover[:5],
        workspace_cleaned=not Path(workspace).exists())
    return base


def _persist_test(role: str, status_str: str, state_root: Path | None, result: dict) -> dict:
    import datetime as _dt
    data = _load_readiness(state_root)
    # Record whether this outcome came from a REAL live request. A non-live config/executable probe
    # (live=False) must never be mistaken for end-to-end auth verification by doctor/status.
    data[role] = {"status": status_str, "live": bool(result.get("live")),
                  "at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")}
    try:
        p = _readiness_path(state_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
    except OSError:
        pass
    return result


# ---------------------------------------------------------------------------
# configure / remove — write through the canonical NodeConfig
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# beta.10 Defect 2: OpenAI-compatible guided configuration + tested profiles.
# Profiles are adapter-agnostic: they only pre-fill display + base URLs the user can override.
# We deliberately do NOT bake in a model ID the endpoint may not accept — the guided flow
# validates the chosen model against the endpoint (or a bounded completion).
# ---------------------------------------------------------------------------
OPENAI_COMPATIBLE_PROVIDERS = ("openai_compatible", "openai_compatible_local")

OPENAI_COMPATIBLE_PROFILES: dict[str, dict] = {
    "zai-glm": {
        "display": "Z.AI GLM",
        "base_url": "https://api.z.ai/api/paas/v4/",
        "coding_base_url": "https://api.z.ai/api/coding/paas/v4/",
        "suggested_secret": "ZAI_API_KEY",
        "suggested_model": "glm-4.6",
    },
}


def suggested_secret_name(provider: str) -> str:
    """A provider-appropriate default secret NAME (never Gemini for a non-Gemini provider)."""
    return {
        "gemini_api": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    }.get(provider, "PROVIDER_API_KEY")


def validate_base_url(url: str) -> None:
    """Raise ValueError unless ``url`` is a syntactically valid http(s) endpoint base URL."""
    from urllib.parse import urlparse
    p = urlparse((url or "").strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"base URL must be an http(s) URL with a host, got {url!r}")


def validate_openai_endpoint(base_url: str, model: str, *, api_key_ref: str = "",
                             timeout: float = 8.0) -> dict:
    """Bounded live validation of an OpenAI-compatible endpoint + model BEFORE saving.

    Prefers GET ``{base_url}/models`` (no token spend); if model listing is unsupported, falls
    back to a 1-token chat completion. Returns a normalized record (never the key). ``ok`` is
    True when the endpoint answered and (when listable) the model is present.
    """
    from urllib.parse import urlparse

    import httpx
    validate_base_url(base_url)
    root = base_url.rstrip("/")
    key = os.environ.get(api_key_ref, "") if api_key_ref else ""
    if not key and api_key_ref:
        # resolve from the managed credential file without exposing the value
        path = secret_env_file()
        if path.is_file():
            for ln in path.read_text().splitlines():
                if ln.startswith(f"{api_key_ref}="):
                    key = ln.split("=", 1)[1]
                    break
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    out = {"ok": False, "host": urlparse(root).netloc, "model": model,
           "model_listed": None, "detail": ""}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{root}/models", headers=headers)
            if r.status_code == 200:
                try:
                    ids = {m.get("id") for m in (r.json().get("data") or [])}
                except Exception:  # noqa: BLE001
                    ids = set()
                out["model_listed"] = (model in ids) if ids else None
                if ids and model not in ids:
                    out["detail"] = f"model {model!r} not offered by endpoint"
                    return out
                out["ok"] = True
                out["detail"] = "endpoint reachable; model present" if ids else \
                    "endpoint reachable (model list empty/unsupported)"
                return out
            if r.status_code in (401, 403):
                out["detail"] = "endpoint reachable but rejected the credential (check the key)"
                return out
            # /models unsupported — try a minimal completion
            cr = c.post(f"{root}/chat/completions", headers=headers, json={
                "model": model, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1})
            out["ok"] = cr.status_code == 200
            out["detail"] = ("completion ok" if cr.status_code == 200
                             else f"completion HTTP {cr.status_code}")
            return out
    except Exception as exc:  # noqa: BLE001 — surface a sanitized category, never the key
        out["detail"] = f"endpoint unreachable: {type(exc).__name__}"
        return out


def catgpt_gateway_default_base_url() -> str:
    """The documented default local endpoint for CatGPT-Gateway (single source: the adapter)."""
    from aithernet.coordinator.providers.catgpt_gateway import DEFAULT_BASE_URL
    return DEFAULT_BASE_URL


def _catgpt_model_missing_detail(provider) -> str:
    """Precise, NON-fabricating message when CatGPT-Gateway has no model configured.

    Attempts model discovery (``GET /models``, bounded, tokenless): if the gateway reports models
    the operator must choose one; if discovery is unavailable, say so plainly. A placeholder model
    is never assumed."""
    import asyncio
    try:
        models = asyncio.run(provider.list_models())
    except Exception:  # noqa: BLE001 — discovery is best-effort and must never raise here
        models = []
    if models:
        return ("CatGPT-Gateway model not configured. The gateway offers: "
                + ", ".join(models[:20])
                + " — choose one with `aithernet agents connect coordinator`.")
    return "CatGPT-Gateway model not configured and model discovery is unavailable."


def discover_models(base_url: str, *, api_key_ref: str = "", timeout: float = 6.0) -> list[str]:
    """Best-effort list of model ids from ``GET {base_url}/models`` (no token spend, no secrets).

    Returns [] on any failure (endpoint down, no ``/models`` support, non-JSON) — model discovery
    is advisory for the connect UX and never a hard requirement.
    """
    import httpx
    try:
        validate_base_url(base_url)
    except ValueError:
        return []
    root = base_url.rstrip("/")
    key = os.environ.get(api_key_ref, "") if api_key_ref else ""
    if not key and api_key_ref:
        path = secret_env_file()
        if path.is_file():
            for ln in path.read_text().splitlines():
                if ln.startswith(f"{api_key_ref}="):
                    key = ln.split("=", 1)[1]
                    break
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{root}/models", headers=headers)
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception:  # noqa: BLE001 — discovery must never raise; it only informs the UX
        return []
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [m.get("id") for m in entries if isinstance(m, dict) and m.get("id")]


def _catgpt_managed_status(state_root: Path | None = None) -> tuple[bool, str, str | None]:
    """Detect an Aithernet-managed CatGPT gateway and (if managed) its coarse lifecycle status.

    Returns ``(managed, gateway_status, discovered_model)``. Bounded and fully defensive: any error
    (docker absent, not configured, gateway down) resolves to a safe value and never raises. The
    manager import is lazy to avoid an import cycle (the manager imports this module)."""
    try:
        from aithernet.catgpt.manager import CatGptGatewayManager
        mgr = CatGptGatewayManager(state_root=state_root)
        if not mgr.is_configured():
            return (False, "not_managed", None)
        data = mgr.status(probe=True, timeout=3.0)
        return (True, data.get("gateway_status", "error"), data.get("model"))
    except Exception:  # noqa: BLE001 — status must never fail because of the managed gateway probe
        return (False, "not_managed", None)


def ensure_catgpt_coordinator_model(state_root: Path | None = None, *,
                                    chosen: str | None = None,
                                    discovered: list[str] | None = None) -> dict:
    """beta.7: auto-persist the managed CatGPT gateway's discovered model into the coordinator
    config so a fresh client never runs a manual ``agents configure coordinator --model`` command.

    If the coordinator is the managed ``catgpt_gateway`` and has no configured model, and the
    gateway discovered exactly one model (``catgpt-browser``), persist it. If several models are
    discovered the caller must pass ``chosen``. Fully defensive — never raises. Returns a result
    dict with ``managed``/``persisted``/``reason``/``model``/``discovered_models``.
    """
    try:
        provider = _selected_provider(COORDINATOR, state_root)
        if provider != "catgpt_gateway":
            return {"managed": False, "persisted": False, "reason": "not_catgpt_gateway"}
        cfg = _role_provider_config(COORDINATOR, state_root)
        configured = getattr(cfg, "model", None) or None
        base_url = getattr(cfg, "base_url", None) or catgpt_gateway_default_base_url()
        ref = _api_key_ref(_section(COORDINATOR, state_root)) or "CATGPT_GATEWAY_API_KEY"
        if configured and not chosen:
            return {"managed": True, "persisted": False, "reason": "already_configured",
                    "configured_model": configured, "model": configured}
        if discovered is None:
            discovered = discover_models(base_url, api_key_ref=ref)
        if chosen:
            model = chosen
        elif len(discovered) == 1:
            model = discovered[0]
        elif len(discovered) > 1:
            return {"managed": True, "persisted": False, "reason": "multiple_models",
                    "discovered_models": discovered, "configured_model": configured}
        else:
            return {"managed": True, "persisted": False, "reason": "no_model_discovered",
                    "discovered_models": [], "configured_model": configured}
        configure(COORDINATOR, "catgpt_gateway", base_url=base_url, api_key_ref=ref,
                  model=model, state_root=state_root)
        return {"managed": True, "persisted": True, "model": model,
                "discovered_models": discovered}
    except Exception as exc:  # noqa: BLE001 — auto-repair is best-effort; never break the caller
        return {"managed": True, "persisted": False, "reason": f"error:{type(exc).__name__}"}


def _endpoint_reachable(base_url: str, *, timeout: float = 1.0) -> bool:
    """A bounded, non-sensitive TCP-connect probe of the endpoint host:port (no request sent)."""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse((base_url or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def record_provider_readiness(role: str, status_str: str, *, live: bool = False,
                              state_root: Path | None = None) -> dict:
    """Record a provider readiness outcome (e.g. a fresh ``quota_exhausted``) into the readiness
    cache so ``status``/``mission_ready`` reflect it immediately.

    A non-``ready`` status (or ``live=False``) makes ``live_verified``/``mission_ready`` read
    False until the next successful live test — used by the mission engine to stop over-claiming a
    coordinator after a live quota failure. Never writes any secret.
    """
    return _persist_test(role, status_str, state_root, {"live": live})


def configure(role: str, provider: str, *, model: str = "", executable: str = "",
              base_url: str = "", api_key_ref: str = "", identity_model: str = "",
              state_root: Path | None = None) -> dict:
    """Configure ``role`` by writing the canonical NodeConfig section. The credential is recorded
    only as an `env:VAR` reference — never a value. Migrates a legacy agents.json first."""
    info = get_info(role, provider)
    if info is None:
        valid = ", ".join(p.key for p in providers_for(role))
        raise ValueError(f"unknown {role} provider '{provider}' (choose: {valid})")
    if api_key_ref and ("sk-" in api_key_ref or len(api_key_ref) > 64
                        or api_key_ref.startswith(("AIza", "ghp_"))):
        raise ValueError("pass --api-key-ref the NAME of an env var holding the key, not the key")
    # beta.5: CatGPT-Gateway is a local OpenAI-compatible gateway with a documented default
    # endpoint; apply it when the operator did not pass one. The credential is optional (local).
    # The model is NOT defaulted here — it is chosen from discovery in the connect flow (or set
    # explicitly); if it stays unset, setup/live-test fail clearly rather than fabricate one.
    if provider == "catgpt_gateway":
        base_url = base_url or catgpt_gateway_default_base_url()
        validate_base_url(base_url)
    # beta.10 Defect 2: an OpenAI-compatible provider is unusable without a base URL + model;
    # validate the URL FORMAT before persisting so we never save an unreachable selection.
    elif provider in OPENAI_COMPATIBLE_PROVIDERS:
        if not base_url:
            raise ValueError(f"{provider} requires --base-url (the endpoint base URL)")
        validate_base_url(base_url)
        if not model:
            raise ValueError(f"{provider} requires --model (the model ID the endpoint accepts)")
    migrate_agents_json(state_root)
    data = _read_node_yaml(state_root)
    section: dict = {"provider": provider}
    exe = executable or (info.executable or "")
    if info.kind == "cli" and exe:
        section["executable"] = exe
    if model:
        section["model"] = model
    if base_url:
        section["base_url"] = base_url
    if api_key_ref:
        section["api_key"] = f"env:{api_key_ref}"      # reference, resolved by the loader
    if info.kind == "local":
        # The config field is allow_unauthenticated (a local/self-hosted endpoint needs no key).
        section["allow_unauthenticated"] = True
    data[_SECTION[role]] = section
    _write_node_yaml(data, state_root)
    if identity_model:
        _record_identity_model(identity_model, state_root)
    return {"role": role, "provider": provider, "configured": True,
            "support_level": info.support_level, "preferred_beta": info.preferred_beta,
            "config_source": str(node_config_path(state_root)),
            "next": f"run `aithernet agents test {role}` to verify readiness"}


def _record_identity_model(identity_model: str, state_root: Path | None) -> None:
    if identity_model not in IDENTITY_MODELS:
        return
    try:
        from aithernet.provisioning import state as sstate
        root = _state_root(state_root)
        st = sstate.load_state(root) or sstate.SetupState()
        st.identity_model = identity_model
        sstate.save_state(st, root)
    except Exception:  # noqa: BLE001
        pass


def remove(role: str, *, state_root: Path | None = None) -> dict:
    """Reset a role to disabled in the canonical NodeConfig (no credentials were ever stored)."""
    data = _read_node_yaml(state_root)
    data[_SECTION[role]] = {"provider": DISABLED}
    _write_node_yaml(data, state_root)
    return {"role": role, "removed": True, "provider": DISABLED,
            "config_source": str(node_config_path(state_root))}


# ---------------------------------------------------------------------------
# One-time idempotent migration of a legacy agents.json into NodeConfig
# ---------------------------------------------------------------------------
def migrate_agents_json(state_root: Path | None = None) -> dict:
    """Fold a legacy ``agents.json`` selection into the canonical NodeConfig (idempotent).

    Only fills a NodeConfig section that is absent/disabled; an existing canonical selection that
    DISAGREES is reported as a conflict and left untouched (NodeConfig wins). The legacy file is
    renamed to ``agents.json.migrated`` so it can never become a second source of truth.
    """
    legacy = _state_root(state_root) / "config" / "agents.json"
    out = {"migrated": [], "conflicts": [], "legacy_present": legacy.is_file()}
    if not legacy.is_file():
        return out
    try:
        legacy_data = json.loads(legacy.read_text())
    except (OSError, ValueError):
        return out
    data = _read_node_yaml(state_root)
    for role in ROLES:
        leg = legacy_data.get(role) or {}
        leg_provider = leg.get("provider")
        if not leg_provider or leg_provider == DISABLED:
            continue
        existing = (data.get(_SECTION[role]) or {}).get("provider")
        if existing and existing not in ("", DISABLED):
            if existing != leg_provider:
                out["conflicts"].append(
                    {"role": role, "node_config": existing, "agents_json": leg_provider})
            continue  # NodeConfig is authoritative; never overwrite it
        section = {"provider": leg_provider}
        if leg.get("executable"):
            section["executable"] = leg["executable"]
        if leg.get("model"):
            section["model"] = leg["model"]
        if leg.get("base_url"):
            section["base_url"] = leg["base_url"]
        if leg.get("api_key_ref"):
            section["api_key"] = f"env:{leg['api_key_ref']}"
        data[_SECTION[role]] = section
        out["migrated"].append(role)
    if out["migrated"]:
        _write_node_yaml(data, state_root)
    try:
        legacy.rename(legacy.with_suffix(".json.migrated"))
    except OSError:
        pass
    return out
