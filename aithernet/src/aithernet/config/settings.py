"""Typed node configuration model."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class CoordinatorConfig(BaseModel):
    """Configuration for the coordinator agent's model provider.

    Fields that select a model and authenticate are optional so a node can start
    without a coordinator configured; in that case any attempt to reason fails with a
    clear configuration error rather than fabricating a decision. Values may be wired
    from the environment in ``node.yaml`` using the ``env:VAR_NAME`` syntax (resolved by
    the config loader); an unset variable resolves to ``None``.
    """

    provider: str = Field(
        default="openai_compatible",
        description="Provider implementation name (e.g. openai_compatible, anthropic, gemini_cli).",
    )
    model: str | None = Field(default=None, description="Model identifier to request.")
    fallback_providers: list[str] = Field(
        default_factory=list,
        description="beta.10 Defect 4: ordered coordinator fallback chain used when the primary "
        "provider's quota is exhausted. Each entry is a provider key (e.g. 'gemini_cli') or "
        "'provider:profile' (e.g. 'openai_compatible:zai-glm'). Switches are explicit and recorded "
        "in mission provenance; never silent.",
    )
    timeout_seconds: float = Field(
        default=180.0, gt=0,
        description="Per-request timeout for provider calls. beta.9: the default is well above the "
        "demonstrated 60s readiness/planning boundary so a full MissionEngine planning prompt does "
        "not time out where a short live probe succeeded.",
    )
    temperature: float | None = Field(
        default=0.2, description="Sampling temperature, if the provider supports it."
    )
    max_tokens: int = Field(
        default=1024, gt=0, description="Maximum tokens to generate (used by Anthropic)."
    )

    # -- HTTP-API providers (openai_compatible, anthropic) --
    base_url: str | None = Field(
        default=None, description="Provider API base URL (provider-specific default if unset)."
    )
    api_key: str | None = Field(
        default=None, description="API key value (typically wired from an env var)."
    )
    allow_unauthenticated: bool = Field(
        default=False,
        description="Permit calling a provider without an API key (local/self-hosted servers).",
    )

    # -- cloud providers (anthropic_bedrock, anthropic_vertex, gemini_vertex) — beta.4 --
    region: str | None = Field(
        default=None,
        description="Cloud region/location for Bedrock/Vertex (e.g. 'us-east-1', 'us-east5').",
    )
    project: str | None = Field(
        default=None, description="Google Cloud project id for Vertex providers."
    )

    # -- CLI providers (gemini_cli) --
    executable: str = Field(
        default="gemini",
        description="CLI executable name or path for CLI-based providers (e.g. gemini_cli).",
    )
    working_directory: str | None = Field(
        default=None,
        description=(
            "Optional override for a CLI provider's dedicated, sandboxed runtime directory. "
            "When unset, the provider uses an empty directory under Aithernet's own state "
            "area (never the repo root, coding workspace, gr-mcp checkout, or HOME)."
        ),
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description="Additional CLI arguments passed verbatim to a CLI provider.",
    )

    @field_validator("provider", "executable", "timeout_seconds", "model", mode="before")
    @classmethod
    def _fall_back_to_default(cls, value, info):
        """Treat an unset/blank env-resolved value as the field default.

        Lets ``node.yaml`` wire these from the environment (``env:AITHERNET_COORDINATOR_*``)
        while keeping the node usable: an unset variable (``None``) or an empty string falls
        back to the documented default. A blank ``model`` therefore means "let the provider
        choose its default model", never an invalid empty model id.
        """
        if value is None or value == "":
            default = cls.model_fields[info.field_name].default
            # ``model`` has no concrete default (None) — keep it None so it reads as "unset".
            return default
        return value


class CodingAgentConfig(BaseModel):
    """Configuration for the coding-agent execution worker.

    The Stage 3 provider is Claude Code run through its CLI. ``executable`` and
    ``workspace`` may be wired from the environment in ``node.yaml`` via ``env:VAR_NAME``;
    an unset variable resolves to ``None`` and falls back to the field default (so a node
    is usable out of the box while still allowing overrides).
    """

    provider: str = Field(
        default="claude_code", description="Coding-agent provider implementation name."
    )
    executable: str = Field(
        default="claude", description="Coding-agent CLI executable name or path."
    )
    workspace: str = Field(
        default="./workspace",
        description="Directory the coding agent runs in (created if missing).",
    )
    timeout_seconds: float = Field(
        default=600.0, gt=0, description="Hard timeout for a single coding-agent run."
    )
    output_format: str = Field(
        default="text", description="CLI output format: text, json, or stream-json."
    )
    extra_args: list[str] = Field(
        default_factory=list, description="Additional CLI arguments passed verbatim."
    )
    model: str | None = Field(
        default=None, description="Model to pass to the coding agent, if supported."
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for the subprocess (never reported).",
    )
    # -- beta.4: API-key coding profile + local endpoint + explicit fallback chain --
    api_key: str | None = Field(
        default=None,
        description="API-key reference (env:VAR_NAME) for the Codex/OpenAI API coding profile.",
    )
    base_url: str | None = Field(
        default=None, description="Base URL for a local/OpenAI-compatible coding endpoint."
    )
    fallback_providers: list[str] = Field(
        default_factory=list,
        description="beta.4: ordered coding fallback chain used on a provider-availability "
        "condition (quota/subscription/credit/endpoint). Switches are explicit and recorded; "
        "each fallback uses its OWN configuration and secret — never another provider's credential.",
    )

    @field_validator("provider", "executable", "workspace", mode="before")
    @classmethod
    def _fall_back_to_default(cls, value, info):
        """Treat an unset (``None``) env-resolved value as the field default.

        This lets ``node.yaml`` wire ``provider``/``executable``/``workspace`` from the
        environment (``env:AITHERNET_CODING_AGENT_*``) while keeping the node usable out of
        the box: an unset variable falls back to the documented default rather than failing
        validation or silently selecting a different provider than requested.
        """
        if value is None:
            return cls.model_fields[info.field_name].default
        return value


class MCPSessionConfig(BaseModel):
    """Lifecycle settings for the persistent, long-lived MCP session (Stage 11B).

    These are infrastructure recovery limits, NOT autonomy modes: a persistent session may
    execute many atomic calls, but it never replays a failed call and never executes an
    unrequested sequence of mission actions.
    """

    autostart: bool = Field(
        default=True, description="Start the MCP session on node startup when configured."
    )
    auto_restart: bool = Field(
        default=True,
        description="Attempt bounded recovery if the subprocess dies unexpectedly.",
    )
    max_restart_attempts: int = Field(
        default=3, ge=0, description="Max consecutive auto-restart attempts before giving up."
    )
    restart_initial_backoff_seconds: float = Field(
        default=1.0, gt=0, description="Initial backoff before the first auto-restart."
    )
    restart_max_backoff_seconds: float = Field(
        default=10.0, gt=0, description="Maximum backoff between auto-restart attempts."
    )
    shutdown_grace_seconds: float = Field(
        default=5.0, gt=0, description="Grace period before killing the subprocess on stop."
    )
    max_in_flight_requests: int = Field(
        default=1,
        ge=1,
        description="Max concurrent JSON-RPC requests (conservative default of 1).",
    )
    stderr_buffer_bytes: int = Field(
        default=256 * 1024,
        gt=0,
        description="Max bytes of subprocess stderr retained in memory (bounded ring).",
    )


class MCPServerConfig(BaseModel):
    """Configuration for an external MCP server (Stage 4).

    The Stage 4 client is a generic stdio MCP client; the intended/default external
    server is the ready-made ``yoelbassin/gr-mcp`` GNU Radio MCP server. Fields may be
    wired from the environment in ``node.yaml`` via ``env:VAR_NAME``; an unset variable
    resolves to ``None`` (in ``args`` it stays ``None``), leaving the server unconfigured
    so any tool call fails with a clear error rather than faking GNU Radio behavior. The
    ``env`` map is never reported by the status endpoint.

    The shape mirrors the gr-mcp stdio configuration, e.g.::

        command: uv
        args: ["--directory", "/path/to/gr-mcp", "run", "main.py"]
    """

    provider: str = Field(
        default="stdio", description="MCP client implementation name (e.g. stdio)."
    )
    command: str | None = Field(
        default=None, description="Executable that launches the MCP server (e.g. uv)."
    )
    args: list[str | None] = Field(
        default_factory=list,
        description="Arguments for the server command; unresolved env refs stay None.",
    )
    cwd: str | None = Field(
        default=None, description="Working directory for the server process, if any."
    )
    timeout_seconds: float = Field(
        default=30.0, gt=0, description="Per-operation timeout for MCP requests."
    )
    stdio_stream_limit_bytes: int = Field(
        default=16 * 1024 * 1024,
        gt=0,
        description=(
            "Max size of a single newline-delimited JSON-RPC frame read from the MCP "
            "server's stdout/stderr. asyncio's default (64 KiB) is too small for tools "
            "like gr-mcp's block catalog; oversized frames raise a clear MCPClientError "
            "instead of crashing the backend. Increase if a server legitimately returns "
            "larger frames."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for the subprocess (never reported).",
    )
    session: MCPSessionConfig = Field(
        default_factory=MCPSessionConfig,
        description="Persistent MCP session lifecycle settings (Stage 11B).",
    )


#: The stable id of the legacy GNU Radio MCP backend (never a filesystem path).
LEGACY_RF_BACKEND_ID = "legacy_gr_mcp"


class RFBackendConfig(BaseModel):
    """One configured RF backend reached over its own MCP stdio subprocess (Stage 13A.5).

    A backend is an EXTERNAL process — Aithernet communicates with it only through MCP and
    never imports its Python modules. The legacy GNU Radio backend reuses the existing
    ``gnuradio_mcp`` config as its source of truth; additional backends (e.g. Marconi) supply
    their own command/cwd/workspace here. ``environment`` values are never reported.
    """

    display_name: str = Field(default="RF backend")
    kind: str = Field(default="mcp_stdio", description="Backend transport kind.")
    enabled: bool = Field(default=True)
    experimental: bool = Field(default=False)
    autostart: bool = Field(default=False, description="Start this backend on node startup.")
    command: str | None = Field(default=None)
    args: list[str | None] = Field(default_factory=list)
    cwd: str | None = Field(default=None)
    workspace: str | None = Field(
        default=None, description="Backend artifact workspace root (containment boundary)."
    )
    environment: dict[str, str | None] = Field(
        default_factory=dict,
        description="Extra subprocess env (never reported); unresolved env refs stay None.",
    )
    source_revision: str | None = Field(
        default=None, description="Operator-pinned backend source revision, if known."
    )
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_in_flight_requests: int = Field(default=1, ge=1)
    stdio_stream_limit_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    session: MCPSessionConfig | None = Field(
        default=None, description="Optional explicit session lifecycle overrides."
    )

    def to_mcp_server_config(self) -> MCPServerConfig:
        """Build the MCP stdio :class:`MCPServerConfig` used to launch this backend."""
        session = self.session or MCPSessionConfig()
        session = session.model_copy(
            update={
                "autostart": self.autostart,
                "max_in_flight_requests": self.max_in_flight_requests,
            }
        )
        # Drop env entries whose env:VAR reference is unset (None) — never pass None to the
        # subprocess environment.
        env = {key: value for key, value in (self.environment or {}).items() if value is not None}
        return MCPServerConfig(
            provider="stdio",
            command=self.command,
            args=list(self.args),
            cwd=self.cwd,
            timeout_seconds=self.timeout_seconds,
            stdio_stream_limit_bytes=self.stdio_stream_limit_bytes,
            env=env,
            session=session,
        )


class RFBackendsConfig(BaseModel):
    """Multi-RF-backend registry configuration (Stage 13A.5, additive).

    When absent, the node synthesizes a single ``legacy_gr_mcp`` backend from the existing
    ``gnuradio_mcp`` configuration, so legacy deployments keep working with no changes. The
    delivered posture keeps the legacy backend the stable default and Marconi experimental
    and non-autostart.
    """

    default_backend: str = Field(default=LEGACY_RF_BACKEND_ID)
    backends: dict[str, RFBackendConfig] = Field(default_factory=dict)
    require_default_ready: bool = Field(
        default=False,
        description=(
            "Stage 14A: when true, the default RF backend must reach a ready/degraded state for "
            "node readiness. Default false so a single RF backend failure degrades (never downs) "
            "the node; an experimental non-autostart backend never affects readiness either way."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> RFBackendsConfig:
        for backend_id in self.backends:
            # Backend ids are stable identifiers, never filesystem paths.
            if "/" in backend_id or "\\" in backend_id or not backend_id.strip():
                raise ValueError(f"Invalid RF backend id '{backend_id}'.")
        # The default backend must be the legacy backend or an explicitly configured one.
        if (
            self.default_backend != LEGACY_RF_BACKEND_ID
            and self.default_backend not in self.backends
        ):
            raise ValueError(
                f"default_backend '{self.default_backend}' is not a configured backend."
            )
        return self


class MissionBudgets(BaseModel):
    """Infrastructure resource budgets for one autonomous mission run (Stage 12).

    These are runaway-prevention limits, NOT autonomy modes or mission presets. Per-run
    overrides are validated against these defaults as maximums.
    """

    max_iterations: int = Field(default=25, ge=1)
    max_elapsed_seconds: float = Field(default=1800.0, gt=0)
    max_coordinator_calls: int = Field(default=25, ge=1)
    max_node_state_actions: int = Field(default=10, ge=0)
    max_coding_agent_actions: int = Field(default=5, ge=0)
    max_mcp_actions: int = Field(default=15, ge=0)
    max_response_actions: int = Field(default=10, ge=0)
    max_rf_device_actions: int = Field(
        default=10, ge=0, description="Max model-selected rf_device lease actions (Stage 14B)."
    )
    max_consecutive_failures: int = Field(default=3, ge=1)
    max_context_characters: int = Field(default=100_000, gt=0)
    # beta.9 Defect 3: bounded exponential backoff between transient coordinator retries, instead
    # of three immediate identical retries. Lease renewal continues during the wait.
    coordinator_retry_initial_backoff_seconds: float = Field(default=2.0, ge=0)
    coordinator_retry_max_backoff_seconds: float = Field(default=30.0, ge=0)


class MissionContextLimits(BaseModel):
    """Compact-context assembly limits (Stage 12) — prefer summaries + references."""

    recent_steps: int = Field(default=20, ge=1)
    recent_mcp_calls: int = Field(default=10, ge=0)
    recent_coding_tasks: int = Field(default=10, ge=0)
    recent_events: int = Field(default=20, ge=0)
    result_preview_characters: int = Field(default=8000, gt=0)
    max_total_characters: int = Field(default=100_000, gt=0)


class MissionExecutionConfig(BaseModel):
    """Autonomous mission-execution settings (Stage 12). Infrastructure defaults only."""

    enabled: bool = Field(default=True)
    worker_count: int = Field(default=1, ge=1, description="Max concurrent autonomous missions.")
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    lease_duration_seconds: float = Field(default=30.0, gt=0)
    lease_renew_interval_seconds: float = Field(default=10.0, gt=0)
    shutdown_grace_seconds: float = Field(default=15.0, gt=0)
    recovery_enabled: bool = Field(default=True)
    reassess_after_failure: bool = Field(
        default=True, description="Let the coordinator reassess after a failed route."
    )
    default_budgets: MissionBudgets = Field(default_factory=MissionBudgets)
    context: MissionContextLimits = Field(default_factory=MissionContextLimits)

    @model_validator(mode="after")
    def _validate_intervals(self) -> MissionExecutionConfig:
        if self.lease_renew_interval_seconds >= self.lease_duration_seconds:
            raise ValueError(
                "lease_renew_interval_seconds must be shorter than lease_duration_seconds."
            )
        return self


class IdentityConfig(BaseModel):
    """Local cryptographic node-identity settings (Stage 13A).

    The private key lives in a dedicated identity directory OUTSIDE the repository (an XDG
    state location by default), never in the SQLite database, and is never returned/printed.
    """

    state_directory: str | None = Field(
        default=None,
        description=(
            "Directory holding the node identity key files. Defaults to an XDG state path "
            "(~/.local/state/aithernet/identity) outside the repository when unset."
        ),
    )
    auto_initialize: bool = Field(
        default=False,
        description="Generate a missing identity automatically on startup (off by default).",
    )


class TransportInboundConfig(BaseModel):
    """Authenticated inbound agent-transport settings (Stage 13A)."""

    enabled: bool = Field(default=True)
    maximum_payload_bytes: int = Field(default=1_048_576, ge=1024)
    accepted_clock_skew_seconds: float = Field(default=120.0, ge=0)


class TransportOutboundConfig(BaseModel):
    """Durable outbound delivery-worker settings (Stage 13A)."""

    enabled: bool = Field(default=True)
    worker_count: int = Field(default=1, ge=1)
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    delivery_lease_seconds: float = Field(default=30.0, gt=0)
    shutdown_grace_seconds: float = Field(default=10.0, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    maximum_response_bytes: int = Field(default=1_048_576, ge=1024)
    max_attempts: int = Field(default=5, ge=1)
    initial_backoff_seconds: float = Field(default=1.0, gt=0)
    maximum_backoff_seconds: float = Field(default=60.0, gt=0)
    jitter_seconds: float = Field(default=1.0, ge=0)
    recovery_enabled: bool = Field(default=True)


class TransportLocalDevelopmentConfig(BaseModel):
    """Local-development relaxations (never the default for remote HTTPS peers)."""

    allow_insecure_http: bool = Field(
        default=True,
        description="Permit plain-HTTP peer endpoints for local development (TLS still "
        "verified for HTTPS).",
    )


class AgentTransportConfig(BaseModel):
    """Authenticated inter-node agent transport (Stage 13A). Infrastructure only.

    This stage establishes identity + reliable signed delivery. It does NOT let the
    coordinator choose peers or resume missions from replies (Stage 13B), and it never
    executes inbound messages as missions/tools.
    """

    enabled: bool = Field(default=True)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    inbound: TransportInboundConfig = Field(default_factory=TransportInboundConfig)
    outbound: TransportOutboundConfig = Field(default_factory=TransportOutboundConfig)
    local_development: TransportLocalDevelopmentConfig = Field(
        default_factory=TransportLocalDevelopmentConfig
    )

    @model_validator(mode="after")
    def _validate_backoff(self) -> AgentTransportConfig:
        if self.outbound.maximum_backoff_seconds < self.outbound.initial_backoff_seconds:
            raise ValueError(
                "outbound.maximum_backoff_seconds must be >= initial_backoff_seconds."
            )
        return self


class CommunicationConfig(BaseModel):
    """Coordinator-driven peer messaging settings (Stage 13B). Bounds + timeouts only.

    These are application-level limits layered on top of the Stage 13A signed transport. The
    coordinator decides WHETHER to message a peer and WHAT to say; this only bounds size and
    deadlines. Inbound coordinator requests are OFF by default per peer (trust alone never
    authorizes remote mission creation).
    """

    enabled: bool = Field(default=True)
    max_text_characters: int = Field(default=8_000, ge=1)
    max_data_bytes: int = Field(default=32_768, ge=1)
    max_data_depth: int = Field(default=8, ge=1)
    default_reply_deadline_seconds: float = Field(default=600.0, gt=0)
    max_reply_deadline_seconds: float = Field(default=86_400.0, gt=0)
    timeout_poll_interval_seconds: float = Field(default=5.0, gt=0)
    inbound_requests_enabled: bool = Field(
        default=True,
        description="Master switch for accepting authorized inbound coordinator requests.",
    )
    default_max_inbound_request_bytes: int = Field(default=65_536, ge=1)
    default_max_concurrent_inbound_missions: int = Field(default=4, ge=1)
    recent_conversation_messages: int = Field(default=10, ge=1)

    # -- Stage 13D.3 remote mission-status synchronization --
    mission_status_enabled: bool = Field(
        default=True, description="Master switch for emitting/accepting remote mission status."
    )
    status_freshness_seconds: float = Field(
        default=300.0, gt=0,
        description="A non-terminal remote snapshot becomes 'stale' this long after its last "
        "accepted update. Terminal snapshots are never marked stale.",
    )
    recent_status_events: int = Field(default=20, ge=1)
    default_max_active_remote_snapshots: int = Field(default=64, ge=1)


class ArtifactConfig(BaseModel):
    """Distributed RF-artifact transfer + managed content-addressed store (Stage 13D).

    Artifact transfer is OFF by default per peer (transport trust never authorizes a transfer).
    Binary bytes live ONLY in the managed store on disk — never in SQLite, events, prompts, or
    dashboard JSON. All bounds are conservative; the worker is independent of the mission and RF
    workers, so an artifact transfer never restarts an RF backend.
    """

    enabled: bool = Field(default=True, description="Master switch for artifact transfer.")
    state_directory: str | None = Field(
        default=None,
        description="Managed artifact-store root (OUTSIDE the repo). Defaults to "
        "~/.local/state/aithernet/artifact-store when unset.",
    )
    max_artifact_bytes: int = Field(
        default=512 * 1024 * 1024, ge=1, description="Per-artifact hard size ceiling."
    )
    total_store_quota_bytes: int = Field(
        default=8 * 1024 * 1024 * 1024, ge=1, description="Total managed-store disk quota."
    )
    default_per_peer_quota_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024, ge=1,
        description="Default per-peer transferred-bytes quota.",
    )
    minimum_free_bytes: int = Field(
        default=512 * 1024 * 1024, ge=0,
        description="Refuse new transfers when free disk would drop below this.",
    )
    chunk_bytes: int = Field(default=1 * 1024 * 1024, ge=1, le=16 * 1024 * 1024,
                             description="Streaming chunk size for range transfers.")
    grant_ttl_seconds: float = Field(default=900.0, gt=0, description="Transfer-grant validity.")
    partial_expiry_seconds: float = Field(
        default=86_400.0, gt=0, description="Abandoned partial files are cleaned up after this."
    )
    worker_enabled: bool = Field(default=True, description="Run the background transfer worker.")
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_chunk_delay_seconds: float = Field(
        default=0.0, ge=0,
        description="Test-only deterministic per-chunk download delay. PRODUCTION DEFAULT 0 "
        "(no delay); only a restart/resume integration test sets it > 0.",
    )
    max_concurrent_transfers: int = Field(
        default=2, ge=1, description="Global transfer concurrency."
    )
    default_max_concurrent_transfers_per_peer: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=8, ge=1, description="Transient-failure retry ceiling.")
    initial_backoff_seconds: float = Field(default=1.0, gt=0)
    maximum_backoff_seconds: float = Field(default=120.0, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    remote_status_stale_seconds: float = Field(
        default=120.0, gt=0, description="A remote mission snapshot is 'stale' after this."
    )


class HardwareDiscoveryProviderConfig(BaseModel):
    """One configured hardware discovery provider (Stage 14B, Part B/S).

    ``kind`` selects the provider implementation: ``static`` (operator-declared inline device
    descriptors), ``static_file`` (operator-declared descriptors in a JSON file), ``soapy``
    (SoapySDR command discovery), ``uhd`` (UHD/USRP discovery), or ``command`` (a generic custom
    discovery command emitting a strict JSON schema on stdout). A provider whose tool is absent
    reports unavailable/degraded — it never crashes node startup unless ``required`` is set.

    No production provider invents devices: ``soapy``/``uhd``/``command`` shell out to a real
    tool; ``static``/``static_file`` only surface operator-declared devices. ``environment``
    values are never reported.
    """

    kind: str = Field(default="static", description="static|static_file|soapy|uhd|command")
    enabled: bool = Field(default=True)
    required: bool = Field(
        default=False,
        description="When true, this provider must be available for node readiness.",
    )
    command: str | None = Field(
        default=None, description="Discovery command for soapy/uhd/command kinds."
    )
    args: list[str] = Field(default_factory=list)
    descriptors_file: str | None = Field(
        default=None, description="JSON descriptors file for static_file kind."
    )
    devices: list[dict] = Field(
        default_factory=list, description="Inline operator-declared descriptors for static kind."
    )
    environment: dict[str, str | None] = Field(
        default_factory=dict, description="Extra discovery-subprocess env (never reported)."
    )
    timeout_seconds: float = Field(default=15.0, gt=0)


class HardwareBindingConfig(BaseModel):
    """An administrator-approved device->RF-backend binding (Stage 14B, Part I/S).

    ``device_selector`` matches a persisted device by hardware_key, serial, or
    ``vendor:product`` — never a raw path. ``device_args`` are the bounded, operator-approved
    backend-specific arguments the binding adapter passes to the RF backend; the coordinator can
    never supply these.
    """

    device_selector: str = Field(description="hardware_key | serial | vendor:product to match.")
    backend_id: str = Field(description="RF backend id this device binds to.")
    device_args: dict = Field(default_factory=dict)
    enabled: bool = Field(default=True)
    notes: str | None = Field(default=None)


class HardwareRequiredDeviceConfig(BaseModel):
    """An administrator-declared required device that gates readiness when missing (Part M/S)."""

    selector: str = Field(description="hardware_key | serial | vendor:product to match.")
    description: str | None = Field(default=None)


class HardwareQualificationConfig(BaseModel):
    """Real-hardware qualification + RF execution settings (Stage 14C.1).

    RF flowgraph execution runs in a SEPARATE, independently configurable interpreter (GNU Radio is
    typically installed for the system Python, not Aithernet's venv) — Aithernet NEVER imports
    ``gnuradio`` into its own venv. Captures are bounded; large sample bytes go to the artifact
    store, never SQLite.
    """

    gnuradio_python: str = Field(
        default="/usr/bin/python3",
        description="Interpreter that can import gnuradio + gnuradio.soapy for RF execution.",
    )
    capture_timeout_seconds: float = Field(default=60.0, gt=0)
    default_sample_rate: float = Field(default=2_000_000.0, gt=0)
    default_gain_db: float = Field(default=40.0)
    default_duration_seconds: float = Field(default=3.0, gt=0, le=30.0)
    max_duration_seconds: float = Field(default=10.0, gt=0)
    default_rx_frequency_hz: float = Field(default=2_437_000_000.0, gt=0)
    default_antenna: str | None = Field(default=None)
    stream_format: str = Field(default="CF32", description="Capture sample format (CF32 IQ).")
    retain_raw_capture: bool = Field(
        default=True, description="Keep the raw IQ capture artifact (vs reduced result only)."
    )
    # 2.4 GHz survey defaults (Wi-Fi channel centers, MHz).
    survey_channel_centers_mhz: list[float] = Field(
        default_factory=lambda: [2412.0, 2437.0, 2462.0],
        description="Center frequencies to sweep for the 2.4 GHz survey (default Wi-Fi 1/6/11).",
    )
    survey_dwell_seconds: float = Field(default=1.0, gt=0)
    survey_occupancy_threshold_db: float = Field(
        default=-70.0, description="Power threshold (dBFS) above which a bin counts as occupied."
    )


class HardwareConfig(BaseModel):
    """Managed SDR hardware inventory + leasing settings (Stage 14B, additive).

    OFF by default — a node with no hardware remains fully usable for simulation, coding,
    coordination, and artifact analysis. When enabled, the inventory worker runs enabled
    discovery providers on a bounded interval, persists factual devices/capabilities/health, and
    the lease service grants durable, conflict-checked, bounded leases that authorize managed
    physical-device use. Inventory polling and lease renewal create no MissionStep.
    """

    enabled: bool = Field(default=False, description="Master switch for managed hardware.")
    providers: dict[str, HardwareDiscoveryProviderConfig] = Field(default_factory=dict)
    discovery_interval_seconds: float = Field(default=60.0, gt=0)
    health_interval_seconds: float = Field(default=30.0, gt=0)
    discovery_on_start: bool = Field(
        default=True, description="Run an initial discovery shortly after startup."
    )
    lease_duration_seconds: float = Field(default=120.0, gt=0)
    lease_renew_interval_seconds: float = Field(default=30.0, gt=0)
    max_lease_duration_seconds: float = Field(default=3600.0, gt=0)
    stale_lease_grace_seconds: float = Field(
        default=30.0, ge=0,
        description="After expiry, an unreconciled active lease becomes orphaned then expired.",
    )
    health_event_min_interval_seconds: float = Field(
        default=30.0, ge=0, description="Rate-limit identical repeated health events per device."
    )
    operator_lease_actions_enabled: bool = Field(
        default=False,
        description="Permit operator (non-coordinator) lease acquire/release from API/CLI/UI.",
    )
    required_devices: list[HardwareRequiredDeviceConfig] = Field(default_factory=list)
    bindings: list[HardwareBindingConfig] = Field(default_factory=list)
    max_devices: int = Field(default=256, ge=1)
    qualification: HardwareQualificationConfig = Field(
        default_factory=HardwareQualificationConfig,
        description="Real-hardware qualification + RF execution settings (Stage 14C.1).",
    )

    @model_validator(mode="after")
    def _validate(self) -> HardwareConfig:
        if self.lease_renew_interval_seconds >= self.lease_duration_seconds:
            raise ValueError(
                "lease_renew_interval_seconds must be shorter than lease_duration_seconds."
            )
        for provider_id in self.providers:
            if "/" in provider_id or "\\" in provider_id or not provider_id.strip():
                raise ValueError(f"Invalid hardware provider id '{provider_id}'.")
        return self


class SoakThresholds(BaseModel):
    """Explicit soak pass/fail thresholds (Stage 14C.2, Part O) — configuration,
            not test constants."""

    min_mission_success_ratio: float = Field(default=0.95, ge=0, le=1)
    min_capture_success_ratio: float = Field(default=0.95, ge=0, le=1)
    min_artifact_integrity_ratio: float = Field(default=1.0, ge=0, le=1)
    max_leaked_leases: int = Field(default=0, ge=0)
    max_orphan_processes: int = Field(default=0, ge=0)
    min_restart_recovery_success: float = Field(default=1.0, ge=0, le=1)
    min_peer_delivery_success: float = Field(default=0.95, ge=0, le=1)
    max_rss_growth_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    max_fd_growth: int = Field(default=256, ge=0)
    min_disk_free_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    max_provider_error_rate: float = Field(default=0.5, ge=0, le=1)


class FieldCampaignProfiles(BaseModel):
    """Configurable campaign sizes (Part E) — never hardcoded into application logic."""

    smoke_iterations: int = Field(default=3, ge=1)
    short_soak_seconds: float = Field(default=1800.0, gt=0)     # 30 min
    standard_seconds: float = Field(default=14400.0, gt=0)      # 4 h
    extended_seconds: float = Field(default=86400.0, gt=0)      # 24 h


class FieldValidationConfig(BaseModel):
    """Multi-node field-operation, fault-injection, and soak validation (Stage 14C.2, additive).

    The read surfaces are always available; disruptive physical actions (campaign start, fault
    injection, physical TX) require explicit operator enablement. Fault injection is OFF by default
    and only ever performs bounded, predefined actions — never an arbitrary shell/network command.
    """

    enabled: bool = Field(default=True, description="Field-validation read surfaces available.")
    operator_actions_enabled: bool = Field(
        default=False, description="Permit campaign start/stop + soak runs (disruptive actions)."
    )
    fault_injection_enabled: bool = Field(
        default=False, description="Validation-mode fault injection (bounded predefined actions)."
    )
    transmit_authorized: bool = Field(
        default=False, description="Physical TX requires explicit operator authorization (Part G)."
    )
    soak_sample_interval_seconds: float = Field(default=5.0, gt=0)
    rest_interval_seconds: float = Field(default=1.0, ge=0)
    capture_sample_rate: float = Field(default=2_000_000.0, gt=0)
    capture_gain_db: float = Field(default=40.0)
    capture_duration_s: float = Field(default=1.0, gt=0, le=10.0)
    capture_frequency_hz: float = Field(default=2_437_000_000.0, gt=0)
    measurement_event_min_interval_seconds: float = Field(default=2.0, ge=0)
    profiles: FieldCampaignProfiles = Field(default_factory=FieldCampaignProfiles)
    thresholds: SoakThresholds = Field(default_factory=SoakThresholds)


class ExternalAgentsAuthConfig(BaseModel):
    """Signed-request authentication settings for external agents (Stage 14D)."""

    signed_requests_enabled: bool = Field(default=True)
    allowed_clock_skew_seconds: int = Field(default=60, ge=1, le=3600)
    nonce_retention_seconds: int = Field(default=600, ge=30, le=86400)
    bearer_tokens_enabled: bool = Field(
        default=False, description="Permit operator-generated hashed bearer credentials."
    )


class ExternalAgentsDeliveryConfig(BaseModel):
    """Durable outbound-delivery worker settings for external agents (Stage 14D)."""

    worker_enabled: bool = Field(default=True)
    concurrency: int = Field(default=4, ge=1, le=64)
    per_agent_concurrency: int = Field(default=2, ge=1, le=32)
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    response_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    maximum_attempts: int = Field(default=8, ge=1, le=100)
    retry_base_seconds: float = Field(default=2.0, gt=0)
    retry_max_seconds: float = Field(default=300.0, gt=0)
    message_retention_seconds: int = Field(default=604800, ge=60)
    dead_letter_retention_seconds: int = Field(default=2592000, ge=60)
    claim_seconds: float = Field(default=30.0, gt=0)
    poll_interval_seconds: float = Field(default=1.0, gt=0)


class ExternalAgentsCallbacksConfig(BaseModel):
    """Signed webhook callback + SSRF-prevention policy (Stage 14D). Conservative by default."""

    enabled: bool = Field(default=False)
    https_required: bool = Field(default=True)
    allow_private_networks: bool = Field(default=False)
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_cidrs: list[str] = Field(default_factory=list)
    allowed_ports: list[int] = Field(default_factory=list)
    redirects_enabled: bool = Field(default=False)
    maximum_redirects: int = Field(default=0, ge=0, le=5)
    maximum_response_bytes: int = Field(default=65536, ge=256)
    maximum_url_length: int = Field(default=2048, ge=16)
    verification_required: bool = Field(
        default=True, description="An endpoint must pass a signed challenge before it is used."
    )


class ExternalAgentsWebsocketConfig(BaseModel):
    """Authenticated WebSocket delivery settings for external agents (Stage 14D)."""

    enabled: bool = Field(default=False)
    maximum_connections: int = Field(default=32, ge=1, le=4096)
    per_agent_connections: int = Field(default=4, ge=1, le=256)
    maximum_frame_bytes: int = Field(default=65536, ge=256)
    heartbeat_seconds: float = Field(default=20.0, gt=0)
    idle_timeout_seconds: float = Field(default=120.0, gt=0)
    replay_retention_seconds: int = Field(default=604800, ge=60)
    outbound_queue_limit: int = Field(default=256, ge=1)


class ExternalAgentsQuotasConfig(BaseModel):
    """Per-agent rate + capacity quotas for external agents (Stage 14D)."""

    submissions_per_minute: int = Field(default=60, ge=1)
    callbacks_per_minute: int = Field(default=600, ge=1)
    subscriptions_per_agent: int = Field(default=32, ge=1)
    pending_messages_per_agent: int = Field(default=1000, ge=1)
    authentication_failures_per_minute: int = Field(default=30, ge=1)


class ExternalAgentsConfig(BaseModel):
    """External-agent interoperability gateway settings (Stage 14D, additive).

    OFF by default. Read surfaces and agent provisioning may be enabled without exposing
    network callbacks; webhook and WebSocket delivery stay disabled until explicitly
    configured. There are no secrets here — credentials are wired through the environment
    and stored hashed.
    """

    enabled: bool = Field(
        default=False, description="Master switch for the external-agent gateway."
    )
    authentication: ExternalAgentsAuthConfig = Field(default_factory=ExternalAgentsAuthConfig)
    delivery: ExternalAgentsDeliveryConfig = Field(default_factory=ExternalAgentsDeliveryConfig)
    callbacks: ExternalAgentsCallbacksConfig = Field(default_factory=ExternalAgentsCallbacksConfig)
    websocket: ExternalAgentsWebsocketConfig = Field(default_factory=ExternalAgentsWebsocketConfig)
    quotas: ExternalAgentsQuotasConfig = Field(default_factory=ExternalAgentsQuotasConfig)
    mission_policy_on_disable: str = Field(
        default="retain",
        description="What happens to an agent's accepted missions when it is disabled.",
    )


class DataCollectionConfig(BaseModel):
    """Per-category collection switches (Stage 14E). Training + raw artifacts default OFF."""

    operational_enabled: bool = Field(default=True)
    product_analytics_enabled: bool = Field(default=False)
    training_trajectories_enabled: bool = Field(default=False)
    raw_artifacts_enabled: bool = Field(default=False)
    #: beta.10 Part 3: research-collection scope. ``off`` (no research spool), or ``owner_full``
    #: (the owner-operated node records all legitimately-observable mission data into the local
    #: append-only research spool). Full collection is NEVER the default for external customers.
    data_collection_mode: str = Field(
        default="off",
        description="off | owner_full — owner_full enables the comprehensive research spool.",
    )
    #: beta.9: research upload destination. ``hosted_owner_archive`` (DEFAULT hosted-client path —
    #: sanitized packages are uploaded to the Aithernet HOSTED ingestion using the enrolled node
    #: identity + a Research Upload Capability; the owner Google Drive archive is managed
    #: server-side and the client never touches Google). ``standalone_drive`` is advanced/standalone
    #: mode where the node uploads to its OWN Google Drive (beta.8 behaviour; not the normal path).
    research_upload_mode: str = Field(
        default="hosted_owner_archive",
        description="hosted_owner_archive (default) | standalone_drive (advanced/local archive).",
    )
    #: beta.9: explicit, withdrawable consent to upload sanitized research/diagnostic records to the
    #: Aithernet owner archive. Recording + hosted upload only happen when this is True.
    research_consent: bool = Field(
        default=False,
        description="Consent to upload sanitized research records to the Aithernet owner archive.",
    )
    #: ISO timestamp of the last consent change (audit; never a secret).
    research_consent_at: str | None = Field(default=None)
    #: beta.8 (retained for standalone_drive mode only): opportunistic client Drive sync.
    research_auto_sync: bool = Field(
        default=False,
        description="[standalone_drive only] package + upload to the node's OWN Google Drive.",
    )


class DataExportConfig(BaseModel):
    """Durable export queue + worker settings (Stage 14E). Export disabled + paused by default."""

    enabled: bool = Field(default=False)
    worker_enabled: bool = Field(default=True)
    paused: bool = Field(default=True)
    maximum_batch_records: int = Field(default=500, ge=1, le=100000)
    maximum_batch_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    claim_seconds: float = Field(default=30.0, gt=0)
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    maximum_attempts: int = Field(default=8, ge=1, le=100)
    retry_base_seconds: float = Field(default=2.0, gt=0)
    retry_max_seconds: float = Field(default=300.0, gt=0)
    minimum_free_disk_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    per_destination_concurrency: int = Field(default=1, ge=1, le=16)
    per_tenant_pending_batches: int = Field(default=1000, ge=1)


class DataPrivacyConfig(BaseModel):
    """Privacy enforcement settings (Stage 14E).

    The pseudonymization key is SECRET-backed: only the env-var NAME (``pseudonymization_key_ref``)
    and the non-secret ``pseudonymization_key_id`` / policy version are stored in config + records.
    The high-entropy master secret (>= 256 bits) is read from the environment at use time, never
    inlined, logged, or backed up. When it is missing the platform FAILS CLOSED for pseudonymized
    export rather than falling back to any public/predictable value.
    """

    pseudonymization_enabled: bool = Field(default=True)
    pseudonymization_key_ref: str | None = Field(
        default=None, description="Env var NAME holding the base64 256-bit master secret."
    )
    pseudonymization_key_id: str = Field(
        default="k1", description="Non-secret key id recorded in lineage for rotation tracing."
    )
    pseudonymization_policy_version: str = Field(default="pp1")
    redaction_policy_version: str = Field(default="r1")
    inspect_before_export: bool = Field(default=True)
    require_training_consent: bool = Field(default=True)
    require_raw_artifact_consent: bool = Field(default=True)


class DataRetentionConfig(BaseModel):
    """Retention windows by category/purpose (Stage 14E)."""

    operational_days: int = Field(default=30, ge=1)
    analytics_days: int = Field(default=90, ge=1)
    training_days: int = Field(default=365, ge=1)
    raw_artifact_days: int = Field(default=180, ge=1)
    delivery_evidence_days: int = Field(default=180, ge=1)
    consent_audit_days: int = Field(default=365, ge=1)


class DataLocalDestinationConfig(BaseModel):
    enabled: bool = Field(default=True)
    root: str | None = Field(default=None, description="Operator-configured archive root.")


class DataIngestionDestinationConfig(BaseModel):
    enabled: bool = Field(default=False)
    base_url: str | None = Field(default=None)
    tenant_id: str | None = Field(default=None)
    node_id: str | None = Field(default=None)
    credential_ref: str | None = Field(
        default=None, description="Env var name holding the node signing key (never inline)."
    )


class DataGoogleDriveDestinationConfig(BaseModel):
    enabled: bool = Field(default=False)
    folder_id: str | None = Field(default=None)
    oauth_credential_ref: str | None = Field(
        default=None, description="Env var name for the OAuth credential JSON path (never inline)."
    )
    encryption_key_ref: str | None = Field(
        default=None, description="Env var name for the base64 AES-256 key (never inline)."
    )


class DataDestinationsConfig(BaseModel):
    local: DataLocalDestinationConfig = Field(default_factory=DataLocalDestinationConfig)
    ingestion: DataIngestionDestinationConfig = Field(
        default_factory=DataIngestionDestinationConfig
    )
    google_drive: DataGoogleDriveDestinationConfig = Field(
        default_factory=DataGoogleDriveDestinationConfig
    )


class DataAirGappedConfig(BaseModel):
    enabled: bool = Field(default=False)
    bundle_root: str | None = Field(
        default=None, description="Local/removable-media root for approved encrypted bundles."
    )


class DataPlatformConfig(BaseModel):
    """Privacy-preserving telemetry, consent, and data-export platform (Stage 14E, additive).

    Local-first: the product is fully functional with this disabled. Export is OFF + paused by
    default; training + raw-artifact collection are OFF and require explicit recorded consent.
    There are NO secrets here — credentials/keys are referenced by env-var name, never inlined.
    Unknown consent state behaves as NOT granted.
    """

    enabled: bool = Field(default=True, description="Data-platform read/collection surfaces.")
    tenant_id: str = Field(default="local", description="Tenant this node belongs to.")
    collection: DataCollectionConfig = Field(default_factory=DataCollectionConfig)
    export: DataExportConfig = Field(default_factory=DataExportConfig)
    privacy: DataPrivacyConfig = Field(default_factory=DataPrivacyConfig)
    retention: DataRetentionConfig = Field(default_factory=DataRetentionConfig)
    destinations: DataDestinationsConfig = Field(default_factory=DataDestinationsConfig)
    air_gapped: DataAirGappedConfig = Field(default_factory=DataAirGappedConfig)


class NodeConfig(BaseModel):
    """Runtime configuration for a single Aithernet node.

    Loaded from ``configs/node.yaml`` (see :mod:`aithernet.config.loader`) with
    optional environment-variable overrides. All fields are required to have a
    concrete value so the runtime never starts in an ambiguous state.
    """

    node_id: str = Field(description="Stable unique identifier for this node.")
    node_name: str = Field(description="Human-readable name for this node.")
    node_state_root: str | None = Field(
        default=None,
        description=(
            "Production state root for this node (Stage 14A). When set, provisioning, preflight, "
            "backups, and diagnostics use a deterministic layout beneath it (config/db/identity/"
            "run/logs/backups/rf/coordinator/coding/artifacts/tmp) instead of repo-relative paths. "
            "The running node still uses the explicit database_url/identity/artifact paths, which "
            "provisioned config points beneath this root."
        ),
    )
    backup_directory: str | None = Field(
        default=None,
        description="Override for the node backup directory (defaults to <state_root>/backups).",
    )
    database_url: str = Field(
        default="sqlite:///./aithernet.db",
        description="SQLAlchemy database URL backing the node state store.",
    )
    host: str = Field(default="127.0.0.1", description="API bind host.")
    port: int = Field(default=8080, description="API bind port.")
    log_level: str = Field(default="info", description="Logging verbosity.")
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        description=(
            "Origins permitted by CORS so a browser dashboard (Stage 8) can call the API. "
            "Defaults to all origins for a local, unauthenticated operator tool; restrict "
            "it in shared deployments."
        ),
    )
    coordinator: CoordinatorConfig = Field(
        default_factory=CoordinatorConfig,
        description="Coordinator agent provider configuration.",
    )
    coding_agent: CodingAgentConfig = Field(
        default_factory=CodingAgentConfig,
        description="Coding agent execution-worker configuration.",
    )
    gnuradio_mcp: MCPServerConfig = Field(
        default_factory=MCPServerConfig,
        description="GNU Radio MCP server connection configuration.",
    )
    mission_execution: MissionExecutionConfig = Field(
        default_factory=MissionExecutionConfig,
        description="Autonomous mission-execution settings (Stage 12).",
    )
    agent_transport: AgentTransportConfig = Field(
        default_factory=AgentTransportConfig,
        description="Authenticated inter-node agent transport settings (Stage 13A).",
    )
    rf_backends: RFBackendsConfig = Field(
        default_factory=RFBackendsConfig,
        description="Multi-RF-backend registry settings (Stage 13A.5, additive).",
    )
    communication: CommunicationConfig = Field(
        default_factory=CommunicationConfig,
        description="Coordinator-driven peer messaging settings (Stage 13B, additive).",
    )
    artifacts: ArtifactConfig = Field(
        default_factory=ArtifactConfig,
        description="Distributed artifact transfer + managed store settings (Stage 13D, additive).",
    )
    hardware: HardwareConfig = Field(
        default_factory=HardwareConfig,
        description="Managed SDR hardware inventory + leasing settings (Stage 14B, additive).",
    )
    field_validation: FieldValidationConfig = Field(
        default_factory=FieldValidationConfig,
        description="Multi-node field-operation + soak validation settings (Stage 14C.2).",
    )
    external_agents: ExternalAgentsConfig = Field(
        default_factory=ExternalAgentsConfig,
        description="External-agent interoperability gateway settings (Stage 14D, additive).",
    )
    data_platform: DataPlatformConfig = Field(
        default_factory=DataPlatformConfig,
        description="Privacy-preserving telemetry/consent/export platform (Stage 14E, additive).",
    )

    @property
    def base_url(self) -> str:
        """Convenience URL the CLI uses to reach a locally running node."""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}"

    @property
    def database_path(self) -> str:
        """Filesystem path for SQLite URLs, or the raw URL otherwise."""
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return self.database_url[len(prefix) :]
        return self.database_url
