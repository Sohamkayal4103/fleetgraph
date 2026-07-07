# Aithernet — Autonomous SDR Node Runtime

Aithernet is a deployable **node runtime** for autonomous software-defined-radio (SDR)
work. A node is a self-contained hardware/software/agent unit that will eventually
combine mission-level reasoning, an implementation/execution worker, GNU Radio access,
durable state, peer communication, and user-facing surfaces.

This repository is being built in stages. **Stages 1–8, 11B, and 12 are complete**: the
deterministic node foundation, the coordinator agent runtime, the coding agent runtime that
executes implementation tasks via Claude Code, GNU Radio MCP integration via a generic stdio
MCP client, mission-step execution that routes one coordinator decision to a supported
capability at a time, MCP integration diagnostics for verifying a real gr-mcp / GNU Radio
connection, an external-agent connection interface that lets an outside agent connect,
send messages, create missions, and trigger one explicit mission step, a browser
**web dashboard** for operating the node over the existing API, a persistent managed MCP
session with GNU Radio workspace context, and the **autonomous mission execution loop** that
runs a sequence of atomic, model-selected steps with dynamic reassessment until the
coordinator concludes the mission.

## Stage 1 scope — node foundation

Stage 1 delivers a real, fully-working node skeleton:

- **Node configuration loader** — typed config from `configs/node.yaml`, with
  `AITHERNET_*` environment-variable overrides and `env:VAR_NAME` references.
- **SQLite-backed state store** — SQLAlchemy 2.0 models, an engine/session bootstrap,
  and repository classes for all persistence.
- **Mission persistence** — submit, list, and read missions.
- **Event persistence** — an append-only runtime event log.
- **Runtime event bus** — in-process pub/sub powering a live event stream.
- **FastAPI service** — health, node status, mission, and event endpoints.
- **Server-Sent Events stream** — live tail of runtime events.
- **Typer CLI** — start the server and drive it as an API client.

## Stage 2 scope — coordinator agent runtime

Stage 2 adds mission-level reasoning:

- **Coordinator runtime** — a provider-agnostic facade (`CoordinatorRuntime`) that turns
  a `CoordinatorInput` into a structured `CoordinatorDecision`.
- **Model-provider abstraction** — an abstract `CoordinatorProvider` with two real
  implementations:
  - **OpenAI-compatible** (`/chat/completions`, the first real provider) and
  - **Anthropic** (Messages API).
  Both use `httpx.AsyncClient` and know nothing about FastAPI or the database.
- **Mission processing** — `POST /missions/{id}/process` builds context from mission,
  node status, and recent events; calls the coordinator; persists the decision as an
  event; and advances the mission status.
- **Coordinator events** — `coordinator.processing_started`, `coordinator.decision`,
  and `coordinator.failed`.
- **Status endpoint** — `GET /coordinator/status` reports provider/model/readiness
  **without exposing secrets**.
- **CLI** — `coordinator status`, `mission process <id>`, and `mission submit --process`.

If the coordinator is not configured (or a provider call fails), the runtime fails with
a clear error and records a `coordinator.failed` event — it never fabricates reasoning.

## Stage 3 scope — coding agent runtime

Stage 3 adds the implementation/execution worker:

- **Coding agent runtime** — a provider-agnostic facade (`CodingAgentRuntime`) that turns
  a `CodingTaskExecutionInput` into a `CodingTaskExecutionResult`.
- **Claude Code provider** — the first real provider runs Claude Code through its CLI in
  non-interactive (`--print`) mode via `asyncio.create_subprocess_exec`, from the
  configured workspace, capturing stdout/stderr/exit code. It contains no database or
  FastAPI code.
- **Coding-task persistence** — `CodingTask` and `CodingTaskResult` tables; create, list,
  read, and run tasks, with the latest result retrievable.
- **Task execution** — `POST /coding-tasks/{id}/run` (and a create-and-run endpoint)
  marks the task running, executes it, persists the result, and advances the status.
- **Coding-task events** — `coding_task.created`, `coding_task.started`,
  `coding_task.completed`, and `coding_task.failed`.
- **Status endpoint** — `GET /coding-agent/status` reports provider/executable/workspace
  and readiness **without exposing the subprocess env or any secrets**.
- **CLI** — `coding-agent status`, `coding-task create|list|show|run`, and
  `coding-task submit` (create + run).
- **Coordinator awareness** — when the coding agent is configured and ready, `coding_agent`
  is promoted into the coordinator's *active* capabilities; otherwise it stays in the
  *future* set. The coordinator does **not** automatically route to the coding agent yet.

If the coding agent is not configured or its executable is unavailable, the runtime
records a `coding_task.failed` event, marks the task failed, and returns a clear error —
it never pretends work was done. A task that runs but exits non-zero is recorded as a
normal failed result (not a node error).

## Stage 4 scope — GNU Radio MCP integration

Stage 4 connects the node to GNU Radio through the Model Context Protocol (MCP):

- **Generic MCP client layer** — a transport-agnostic `MCPClient` abstraction plus a real
  **stdio JSON-RPC client** (`StdioMCPClient`) that launches an external server, runs the
  MCP `initialize` → `notifications/initialized` → `tools/list` / `tools/call` lifecycle,
  captures stderr, respects a timeout, and terminates cleanly. It contains no database or
  FastAPI code.
- **gr-mcp as the intended/default server** — Aithernet connects to the ready-made
  [`yoelbassin/gr-mcp`](https://github.com/yoelbassin/gr-mcp) GNU Radio MCP server over
  stdio. gr-mcp is **not** vendored, copied, or required as a dependency; the client is
  generic so other MCP servers work too.
- **Runtime-discovered tools** — tool names come from `tools/list` at runtime and are
  **never** hardcoded; no GNU Radio scan/flowgraph workflows are baked in.
- **Tool-call persistence** — every call is recorded in an `MCPToolCall` row (caller,
  tool, arguments, status, structured result, error, timestamps) for a full audit trail.
- **MCP events** — `mcp.tool_list.requested|completed|failed` and
  `mcp.tool_call.created|started|completed|failed`.
- **Status endpoint** — `GET /mcp/status` reports the provider, a launch-command summary,
  and readiness **without exposing the subprocess env or any secrets**.
- **API & CLI** — list tools, call a tool, and inspect persisted calls over HTTP and via
  `aithernet mcp ...`.
- **Capability awareness** — when MCP is configured and ready, `gnuradio_mcp` is promoted
  from *future* to *active* in the coordinator's capability context and reflected as an
  available node capability in the coding-agent status.

GNU Radio MCP is accessible to both the coordinator (inspection/supervision/context) and
the coding agent (implementation/debugging context), but Stage 4 does **not** route to it
automatically: the coordinator never auto-executes a decision that mentions MCP, and a
coding task only sees `gnuradio_mcp` if its creator explicitly includes it in
`available_tools`. If MCP is not configured or the server fails, the runtime records an
`mcp.tool_*.failed` event and returns a clear error rather than faking GNU Radio behavior.

## Stage 5 scope — mission-step execution (decision routing)

Stage 5 executes one routed mission step from a coordinator decision:

- **Decision routing contract** — the coordinator *decides* (`next_target` +
  `structured_payload`); a deterministic router (`orchestrator/decision_router.py`)
  *validates and executes* exactly one supported route. Interpretation is by a documented
  contract — never fragile natural-language parsing.
- **Supported routes** — `respond` (record a response), `node_state` (return a
  node/coordinator/coding/MCP status + recent-events summary), `coding_agent` (create and
  run a coding task from the payload), and `gnuradio_mcp` (call an MCP tool from the
  payload). A small alias layer maps flexible coordinator strings (e.g. `user`,
  `future_coding_agent`, `mcp`) onto these route names.
- **Honest outcomes** — a route to an unavailable capability (coding agent/MCP not
  configured) is **blocked**, a malformed payload (e.g. missing `tool_name`) is **failed**,
  an unknown/peer/manager/web-UI target is **blocked**, and `continue_reasoning` is
  **blocked** ("continuous/autonomous loops are not enabled in Stage 5"). None of these is
  ever reported as a fake success.
- **MissionStep persistence** — every step records the full decision, normalized
  `route_target`/`route_action`, status, structured result, and error — a complete audit
  of decision execution.
- **Mission-step events** — `mission_step.started`, `mission_step.completed`,
  `mission_step.failed`, `mission_step.blocked`.
- **API & CLI** — `POST /missions/{id}/step`, `GET /mission-steps[/{id}]`, and
  `aithernet mission step|steps|step-show`.

Reasoning is reused, not duplicated: `run_mission_step` calls the existing
`process_mission_with_coordinator` (which still emits `coordinator.processing_started` /
`coordinator.decision`). A failed routed step does **not** mark the mission failed. This is
one explicit step at a time — not an autonomous loop.

## Stage 6 scope — GNU Radio MCP integration diagnostics

Stage 6 is an **authenticity checkpoint**: it adds tooling to verify whether Aithernet is
genuinely connected to a real external gr-mcp / GNU Radio environment. The production path
is unchanged and unfaked — `Aithernet runtime → stdio MCP client → external gr-mcp server
launched by the configured command → GNU Radio`.

- **Diagnostics report** (`src/aithernet/mcp/diagnostics.py`) — a structured, secret-free
  report of provider, command summary, command resolution, unresolved args, cwd status,
  timeout, readiness, and actionable gr-mcp hints. With `probe=false` (default) it inspects
  configuration **without launching the server**; with `probe=true` it exercises the real
  MCP path (`tools/list`) and captures the tool count/names on success or a structured
  error on failure.
- **API** — `GET /mcp/diagnostics` and `GET /mcp/diagnostics?probe=true` (always HTTP 200;
  a failed probe is reported in the body, not as an error).
- **CLI** — `aithernet mcp diagnostics` and `aithernet mcp diagnostics --probe`.
- **Standalone validator** — `scripts/validate_gr_mcp.py` uses the *same* config loader and
  `MCPRuntime` as the node (no API server required) to print diagnostics, list tools, or
  call a tool against the real configured server.

No fakes: diagnostics never fabricate a tool list, never simulate GNU Radio, never assume
tool names (`aithernet mcp tools` shows the real ones), and never report an unconfigured
server as success.

Every endpoint and CLI command listed here actually functions; there are no stubbed
responses or simulated behavior in production code. Test doubles exist only in the test
suite.

## Stage 7 scope — external agent connection interface

Stage 7 lets an **external agent** connect to the node and interact with it. That agent
may be a user's private agent, a manager agent, another node's coordinator, a lab
orchestrator, or any local/cloud process. External agents are **message/mission sources**,
not internal reasoning providers: they connect, are listed, send messages, optionally
create missions, and optionally trigger **one** explicit mission step — all persisted.

- **Communication layer** (`src/aithernet/communication/`) — a `CommunicationRuntime` that
  owns external-agent connections and messaging, plugged into the existing mission system
  (it reuses `create_mission` and `run_mission_step` rather than forking a parallel path).
  `NodeRuntime` exposes thin delegating methods.
- **Persistence** — `ExternalAgent` (name, type, endpoint_url, transport, auth_type,
  status, metadata, timestamps, last_seen) and `ExternalAgentMessage` (direction,
  message_type, content, payload, optional mission link). Every connection, inbound
  message, and outbound reply is recorded.
- **Connect & roster** — `POST /agents/connect` registers an agent as `connected`;
  `GET /agents`, `GET /agents/{id}`, and `GET /agents/status` (connected/total counts)
  inspect it; `PATCH /agents/{id}/status` (and `/disconnect`, `/disable`) change status.
  Disabling is preferred over hard deletion.
- **Messaging** — `POST /agents/{id}/message` records an inbound message; with
  `create_mission=true` (default) it creates a mission (`source_type="external_agent"`,
  `source_id=<agent id>`), and with `run_step=true` it runs exactly one mission step via
  the Stage 5 flow. An outbound reply summarising the outcome is recorded. `run_step`
  requires `create_mission` (the ambiguous combination is a clean 400).
- **Events** — `external_agent.connected`, `.disconnected`, `.disabled`,
  `.message_received`, `.message_replied`, `.mission_created`, and `.step_ran`.
- **API & CLI** — the routes above plus `GET /agent-messages` (all messages, with
  `agent_id`/`mission_id` filters), and `aithernet agents connect|list|show|status|
  set-status|message|messages`.

No credentials are stored: a connection records only an `auth_type` *hint* (e.g. "none",
"bearer", "custom"); a raw secret smuggled into `metadata` is rejected by key name (the
value is never stored nor echoed). **`endpoint_url` is stored for future use but never
called automatically** — Stage 7 opens no outgoing sessions, polls nothing, and runs no
autonomous loop. An outbound message row is a recorded local summary, not proof of a
network delivery. A disabled agent cannot send messages; a missing agent is a clean 404;
and a `run_step` request with no configured coordinator is a clean 503 — none is ever
treated as a fake success.

## Stage 8 scope — web dashboard

Stage 8 adds a real **browser dashboard** for operating a local node. It is a Vite + React
+ TypeScript single-page app under [`web/`](web/) that talks to the **existing FastAPI
backend** over HTTP/CORS — there is no mock data, no demo mode, no fake missions/agents/MCP
tools, and no hardcoded SDR workflows. If the backend is down or a capability is
unconfigured, the UI shows the real error/status.

- **Backend support (small, additive)** — CORS is enabled (origins from
  `cors_allow_origins`, default `["*"]` for a local operator tool) so the Vite dev server
  can call the API cross-origin; and if a built `web/dist` exists it is served at
  `/dashboard` (optional, never required, never shadows the API). No existing API behavior
  changed.
- **Typed API client** (`web/src/api/`) — every call maps to a real endpoint; a network
  failure becomes "backend unavailable" and an HTTP error carries the backend's real
  `detail` (so 400/502/503 reach the UI). JSON inputs (metadata/payload/args) are validated
  before sending.
- **Panels** — Node status; Mission Composer (*Submit* and *Submit + Run Step*); Live
  Activity (real `EventSource` against `/events/stream`, plus *Refresh recent* via
  `/events`); Missions + one-step execution; External Agents (connect, list, status change,
  message with `create_mission`/`run_step`); GNU Radio MCP (status, diagnostics, **probe**,
  list tools, call a tool, persisted calls); and Coordinator/Coding-Agent status with the
  coding-tasks list and a create-and-run form.
- **Authenticity** — MCP tool names come only from `/mcp/tools` (never hardcoded); the
  browser never calls an agent's `endpoint_url` (neither does the backend); no autonomous
  loops (one explicit step per click); events shown are verbatim from the stream / `/events`.

The dashboard reads its API base URL from `VITE_AITHERNET_API_BASE_URL` (default
`http://127.0.0.1:8080`). See [Run the web dashboard](#run-the-web-dashboard) below. The
backend test suite does **not** require building the frontend.

## Stage 11B scope — persistent MCP session & GNU Radio context

Stage 11B replaces per-operation MCP subprocesses with **one managed, long-lived MCP
session per node**, and adds a structured, persistent **GNU Radio workspace context**.

> **Persistent MCP session ≠ autonomous mission loop.** The session is *infrastructure*:
> it executes many atomic calls over its lifetime, but Gemini still produces only one
> atomic decision per manually invoked mission step. No route silently runs an unrequested
> sequence of actions, and **no failed call is ever replayed**.

- **`MCPSessionManager`** — launches the gr-mcp subprocess once, runs the initialize
  handshake once, discovers + caches tools, reuses the connection for many operations,
  watches process health, recovers within bounded limits, and shuts down cleanly (no orphan
  process). States: `stopped`, `starting`, `ready`, `degraded`, `restarting`, `stopping`,
  `failed`. **Generation** counts how many subprocesses have reached `ready` (it increments
  on each successful start/restart; a failed start does not). One async lifecycle lock
  serializes transitions; `start`/`stop` are idempotent.
- **One stdio connection** — a single stdout reader loop dispatches each JSON-RPC frame to a
  pending-request map keyed by id; stdin writes are serialized; concurrency is bounded by
  `max_in_flight_requests` (default **1**). Notifications never resolve a request future; a
  process death or stream-limit overrun **fails all pending requests honestly** (never
  marked completed, never replayed). stderr is bounded; the large-response limit is preserved.
- **Cached dynamic tool catalog** — `tools/list` runs once after init and is cached for the
  generation; `aithernet mcp tools` uses the cache, `--refresh`/restart re-discovers. Tool
  names are never hardcoded.
- **Autostart / auto-restart** — the session autostarts on node startup when configured (a
  gr-mcp failure marks it `failed`/`degraded` and is reported honestly — the API stays up).
  Auto-restart is bounded (`max_restart_attempts`, backoff). Even when recovery succeeds, the
  failed in-flight request stays failed. The node shuts the session down on exit.
- **GNU Radio context** — a per-node record with compact summaries (active flowgraph, status,
  block/connection/validation/error/execution summaries, freshness/staleness, source session
  generation, and the **source MCP call ids** for the large raw results, which stay in the
  MCP-call records — never duplicated into context). Values are never fabricated:
  unavailable fields are `null`/`unknown`. A single context service interprets GNU Radio tool
  semantics, but only recognizes a known tool **after confirming it is in the live discovered
  catalog**, and only updates from a *successful* result. A mutation marks derived context
  stale; a session restart marks it stale; `gnuradio context refresh` calls only the
  available read-only tools (no execution/mutation) and honestly reports "no active flowgraph".
- **Coordinator context** — the coordinator decision receives the compact MCP session summary
  (state, generation, tool count, tool *names*) and the compact GNU Radio context — never
  full block catalogs or raw results, and never a fixed "if GNU Radio, choose gnuradio_mcp"
  instruction. Gemini remains free to pick any available atomic route.
- **Persistence** — each MCP call additively records `mission_id`, `mission_step_id`,
  `session_id`, `session_generation`, `tool_catalog_generation`, `duration_ms`, and
  `error_type`. Mission-routed calls get their mission/step ids automatically (the
  coordinator never supplies database ids); manual calls leave them null. Every call reaches
  a terminal state (`completed`/`failed`); recovery never duplicates a persisted call.
- **API / CLI / dashboard / events** — `GET/POST /mcp/session[/start|restart|stop]`,
  `GET/POST /gnuradio/context[/refresh]`; `aithernet mcp session …` and
  `aithernet gnuradio context …`; dashboard MCP-session + GNU-Radio-context panels with
  controls (disabled during transitions); and `mcp.session.*` / `gnuradio.context.*` events
  (sanitized — never env, secrets, full stderr, or large results).

This stage does **not** add the autonomous mission execution loop, peer transport, mission
workers, standing missions, or repeated automatic coordinator reasoning.

## Stage 12 scope — autonomous mission execution loop & dynamic reassessment

Stage 12 promotes the node from *one manually-invoked step* to a node that **autonomously
executes a sequence of atomic, model-selected steps** until the coordinator concludes the
mission — or infrastructure stops it.

> **Autonomous loop ≠ predetermined workflow.** The coordinator chooses *one* next action
> after observing the latest real state and result, then reassesses. There is no fixed plan,
> no keyword→route mapping, no mission presets.
>
> **One atomic step ≠ one whole mission.** Each iteration persists exactly **one**
> `MissionStep` — either an *action step* that runs **at most one** route, or a *control-only
> step* (complete/wait/blocked/cancelled). A mission is many such steps, reassessed after
> every result. Completed actions are never replayed; failed actions are never silently
> retried — the coordinator decides what to do next from the persisted result.

The loop:

```
submit/start → acquire mission lease → assemble compact context →
coordinator chooses ONE atomic action OR a lifecycle transition → persist decision →
execute ≤1 route OR a control-only step → persist real result →
update usage + mission/run state → feed result into next context → reassess → repeat
until completed / waiting / paused / blocked / failed / cancelled
```

- **Lifecycle** — additive mission states `received → queued → active → {waiting, paused,
  completed, blocked, failed, cancelled}`, with **one centralized validated transition
  service** (`missions/lifecycle.py`). Terminal states never silently reopen.
- **`MissionExecutionRun`** — a persistent run entity (id, mission/node id, status, lease
  fields, iteration/coordinator/per-route counters, budgets + usage, last step/decision ids,
  waiting/blocked/failure reasons, final response, recovery count, version). A second run for
  a mission only happens through an explicit user start, and already-completed steps are never
  duplicated.
- **Real DB-backed leasing** — each run is claimed with an atomic conditional `UPDATE`
  (SQLite serializes writes, so two workers can never both win). Renewal/release require the
  current unguessable token; a worker stops if renewal is ever lost; an expired lease is
  recoverable after a restart. **Lease tokens are never exposed** by the API, CLI, events, or
  UI. Heartbeat renews well inside `lease_duration_seconds`.
- **`MissionWorkerManager`** — a bounded background worker pool (default **1**) that polls
  queued runs, acquires leases, runs **one task per mission**, recovers interrupted runs on
  startup, and shuts down cleanly at atomic boundaries (no orphan Gemini/Codex/MCP process).
- **`mission_control` decision contract** — additive `{disposition: continue|complete|wait|
  blocked, reason, final_response, waiting, blocked}`. `continue` runs one route; `complete`
  needs a non-empty final response; `wait`/`blocked` run no route and carry structured
  reasons. Invalid combinations (continue-no-target, complete-no-response, wait-with-route, …)
  are rejected and persisted as a clear failure.
- **Resource budgets** — runaway-prevention limits (iterations, elapsed seconds, coordinator
  calls, per-route action caps, consecutive failures, context characters). Checked before each
  coordinator call and route; usage is persisted atomically and never negative. Exhaustion
  persists a control step and **blocks** with reason `resource_budget_exhausted` (never
  "completed"), and does not call the coordinator merely to announce exhaustion. Per-run
  overrides may only *lower* a limit.
- **Pause / resume / waiting / cancel** — pause stops at the next atomic boundary; resume
  requeues and reassesses (steps/usage preserved, **no replay**); waiting does not consume a
  worker; cancel persists the request, prevents new iterations, acknowledges, transitions to
  `cancelled`, and releases the lease — **no coordinator call happens after acknowledgement**.
- **Recovery** — on restart, interrupted `active` runs with an expired lease are requeued for
  a **fresh** coordinator decision; their interrupted `running` steps are marked
  failed/interrupted (never assumed completed, never replayed); terminal/paused/waiting runs
  are preserved; `recovery_count` increments.
- **API / CLI / dashboard / events** — `POST /missions/{id}/{start,pause,resume,cancel,
  run-step}`, `GET /missions/{id}/{execution,timeline}`, `GET /mission-runs[/{id}]`,
  `GET /mission-worker/status` (the legacy manual `POST /missions/{id}/step` is preserved and
  still does **not** auto-continue); `aithernet mission {start,pause,resume,cancel,execution,
  timeline,watch,runs,run-step}` and `aithernet worker status`; a dashboard **Autonomous
  Execution** panel (composer "Submit + Start Autonomous", lifecycle badges, run/lease/budget
  view, Start/Pause/Resume/Cancel/Run-one-step controls, timeline, live updates — read-only
  polling never starts a worker); and `mission.*` / `worker.*` events. **No prompts, raw model
  output, full MCP/Codex bodies, credentials, environment, or lease tokens** appear in events.

Configuration lives under the validated `mission_execution` block in `configs/node.yaml`
(`enabled`, `worker_count`, poll/lease/shutdown intervals, `recovery_enabled`,
`default_budgets`, `context`). Set `enabled: false` to keep only the manual one-step path.

Stage 12 does **not** add peer/manager transport, scheduled/standing missions, cron triggers,
multi-node consensus, remote delegation, or any hardcoded GNU Radio plans — those are Stage 13.

## Intentionally **not** implemented yet

The following remain explicit future integration points (named as `future_*` capabilities,
but never invoked):

- **Peer/manager transport & multi-node coordination** — Stage 12's autonomous loop runs
  **within a single node**; peer-to-peer/manager delegation, scheduled/standing missions,
  cron triggers, and multi-node consensus remain Stage 13 work. Peer/manager *mission-step
  routing* targets are still unsupported (blocked) routes.
- **Outbound callbacks & peer mesh** — Stage 7 adds the *inbound* external-agent
  connection layer (agents connect and send messages to the node), but the node never
  calls an agent's `endpoint_url`, opens a persistent WebSocket, or runs peer-to-peer /
  multi-node coordination. `endpoint_url` is stored for a later stage; peer/manager
  *mission-step routing* targets remain unsupported (blocked) routes for now.
- **Authentication** — the Stage 8 dashboard and the API are unauthenticated (a local
  operator tool); no login, tokens, or per-user access control yet. CORS defaults to all
  origins for local use and should be restricted for any shared deployment.

The runtime is deliberately deterministic *infrastructure*; it imposes no predetermined
mission behavior, no hardcoded SDR workflows, and no fixed autonomy levels. The coordinator
reasons from the supplied state, the coding agent works from the supplied task, MCP tools
are discovered at runtime, and the router only executes a clear, available decision — not
from presets.

## Architecture summary

```
src/aithernet/
├── main.py            # ASGI entrypoint (uvicorn aithernet.main:app)
├── cli.py             # Typer CLI: `aithernet ...`
├── api/               # FastAPI app factory + routers (health, node, coordinator,
│                      #   coding_agent, coding_tasks, mcp, missions, mission_steps,
│                      #   agents, events)
├── config/            # NodeConfig + Coordinator/CodingAgent/MCPServer config, YAML/env loader
├── orchestrator/      # EventBus + NodeRuntime (lifecycle & operations)
│   └── decision_router.py  # route normalization/validation (no execution, no DB)
├── communication/     # External-agent connection layer (Stage 7; NOT a peer mesh)
│   ├── contracts.py   #   ExternalAgent* schemas/enums + errors (no credentials stored)
│   └── runtime.py     #   CommunicationRuntime (connections + messaging via NodeRuntime)
├── coordinator/       # Coordinator agent runtime
│   ├── contracts.py   #   CoordinatorInput / CoordinatorDecision / CoordinatorStatus + errors
│   ├── prompts.py     #   system/user prompt builders
│   ├── runtime.py     #   CoordinatorRuntime (provider-agnostic facade)
│   └── providers/     #   base ABC + openai_compatible + anthropic
├── coding_agent/      # Coding agent runtime
│   ├── contracts.py   #   CodingTaskExecutionInput / ...Result / CodingAgentStatus + errors
│   ├── runtime.py     #   CodingAgentRuntime (provider-agnostic facade)
│   └── providers/     #   base ABC + claude_code (CLI subprocess)
├── mcp/               # GNU Radio MCP client/runtime (NOT a copy of gr-mcp)
│   ├── contracts.py   #   MCPToolInfo / MCPToolCallResult / MCPServerStatus + errors
│   ├── diagnostics.py #   MCPDiagnostics + build_diagnostics (config check + optional probe)
│   ├── runtime.py     #   MCPRuntime (transport-agnostic facade)
│   └── clients/       #   base ABC + stdio (real JSON-RPC subprocess client)
├── state/             # SQLAlchemy models, engine/session bootstrap, repositories
└── schemas/           # Pydantic models (missions, mission_steps, coding, mcp, events, node)

scripts/
└── validate_gr_mcp.py  # standalone real-gr-mcp validator (uses MCPRuntime; no API server)

web/                    # Stage 8 browser dashboard (Vite + React + TypeScript)
├── index.html
├── package.json        # scripts: dev / build / preview / lint
├── vite.config.ts
└── src/
    ├── main.tsx, App.tsx        # single-page dashboard shell + header
    ├── api/                     # typed client (client.ts), types.ts, hooks.ts
    ├── components/              # NodeStatusPanel, MissionComposer, MissionList,
    │                            #   MissionStepPanel, EventStream, AgentPanel, McpPanel,
    │                            #   CodingAgentPanel, StatusCard
    └── styles.css
```

The dashboard is a static SPA that calls the API over CORS at `VITE_AITHERNET_API_BASE_URL`
(default `http://127.0.0.1:8080`); it uses **no** mock data. The backend enables CORS
(`cors_allow_origins`) and, if `web/dist` has been built, serves it at `/dashboard` — an
optional convenience that never alters API behavior and is not required by the tests.

Request flow: routers depend on a single `NodeRuntime` held on `app.state`. The runtime
owns configuration, hands out database sessions, runs operations through repositories,
and publishes events to the bus. The bus feeds the SSE stream.

Coordinator flow: `NodeRuntime.process_mission_with_coordinator` loads the mission and
recent events, builds a `CoordinatorInput`, and calls `CoordinatorRuntime.decide`, which
delegates to the configured `CoordinatorProvider`. The provider calls a real model API
and returns a `CoordinatorDecision`, persisted as a `coordinator.decision` event.

Coding-agent flow: `NodeRuntime.run_coding_task` loads the task, builds a
`CodingTaskExecutionInput`, and calls `CodingAgentRuntime.execute`, which delegates to the
configured `CodingAgentProvider` (Claude Code runs in the workspace as a subprocess). The
returned `CodingTaskExecutionResult` is persisted as a `CodingTaskResult` plus a
`coding_task.completed`/`coding_task.failed` event.

MCP flow: `NodeRuntime.call_mcp_tool` persists an `MCPToolCall`, then calls
`MCPRuntime.call_tool`, which delegates to the configured `MCPClient` (the stdio client
launches the external server, runs the JSON-RPC lifecycle, and calls the tool). The result
is persisted on the call record plus an `mcp.tool_call.completed`/`failed` event.

Mission-step flow: `NodeRuntime.run_mission_step` reuses `process_mission_with_coordinator`
for the decision, then `decision_router.normalize_target` + payload builders interpret it,
and the runtime dispatches to exactly one capability method (respond/node_state, or
`create_and_run_coding_task`, or `call_mcp_tool`). The decision and outcome are persisted as
a `MissionStep` with a `mission_step.completed`/`failed`/`blocked` event. The router holds
only pure normalization/validation — execution stays in the runtime, avoiding any import
cycle.

External-agent flow: `NodeRuntime.receive_external_agent_message` delegates to
`CommunicationRuntime`, which persists the inbound message, refreshes the agent's
`last_seen_at`, and — if requested — calls the existing `create_mission` (stamping
`source_type="external_agent"`) and `run_mission_step` (one Stage 5 step), then records an
outbound reply. The node never calls `endpoint_url`; `CommunicationRuntime` holds a
reference to `NodeRuntime` (typed under `TYPE_CHECKING`) so the import is one-way.

Provider/transport-specific code lives only in `coordinator/`, `coding_agent/`, and
`mcp/clients/`; routing logic lives only in `orchestrator/decision_router.py`; database
code lives only in `state/`/`orchestrator/`. `NodeRuntime` remains the seam where a future
stage will add autonomous multi-step loops.

## Install

Requires Python 3.11+. Using [uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -e ".[dev]"
```

Or with plain pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

Edit `configs/node.yaml`:

```yaml
node_id: "00000000-0000-4000-8000-000000000001"
node_name: "aithernet-node-local"
database_url: "sqlite:///./aithernet.db"
host: "127.0.0.1"
port: 8080
log_level: "info"
```

Any field can be overridden via environment variable, e.g. `AITHERNET_PORT=9000` or
`AITHERNET_DATABASE_URL=sqlite:///./other.db`. Point at a different config file with
`--config path/to.yaml` (CLI) or `AITHERNET_CONFIG=path/to.yaml` (server).

### Automatic `.env` loading

The config loader **automatically loads the repository-root `.env`** before resolving any
`env:VAR_NAME` reference or `AITHERNET_*` override, for `aithernet start`, the CLI client
commands, uvicorn/ASGI startup, and tests using the standard loader. You no longer need to
run `set -a; source .env; set +a`.

- Loading uses `override=False`, so **explicitly exported shell variables always win** over
  `.env` values.
- A missing `.env` is harmless. Only the repo-root `.env` is read — no parent-directory
  search, so unrelated `.env` secrets are never picked up.
- `.env` is git-ignored and its contents are never logged or returned by status endpoints.

Copy [`.env.example`](.env.example) to `.env` and edit it:

```dotenv
# Coordinator — Gemini CLI (authenticated Google account; requires Node 20+):
AITHERNET_COORDINATOR_PROVIDER=gemini_cli
AITHERNET_COORDINATOR_EXECUTABLE=gemini
AITHERNET_COORDINATOR_MODEL=                  # blank -> the CLI's default model
AITHERNET_COORDINATOR_TIMEOUT_SECONDS=300
# ...or OpenAI-compatible (local Ollama): set PROVIDER=openai_compatible and:
AITHERNET_COORDINATOR_BASE_URL=http://127.0.0.1:11434/v1
AITHERNET_COORDINATOR_API_KEY=ollama
# AITHERNET_COORDINATOR_MODEL=qwen2.5:7b

# GNU Radio MCP (external gr-mcp launched over stdio)
AITHERNET_GNURADIO_MCP_COMMAND=/path/to/gr-mcp/.venv/bin/python
AITHERNET_GR_MCP_DIR=/path/to/gr-mcp

# Coding agent (provider and executable MUST match)
AITHERNET_CODING_AGENT_PROVIDER=codex_cli
AITHERNET_CODING_AGENT_EXECUTABLE=codex
AITHERNET_CODING_AGENT_WORKSPACE=$HOME/.local/state/aithernet/workspace
```

### Coordinator provider (Stage 2 / Stage 11A)

The coordinator is configured under a `coordinator:` block; provider selection is
environment-driven. Values written as `env:VAR_NAME` are resolved by the loader; an unset
or blank variable falls back to the field default (provider → `openai_compatible`,
executable → `gemini`). **The coordinator only reasons — Aithernet executes.** It produces
one `CoordinatorDecision` and the deterministic router runs exactly one atomic route
(`respond`, `node_state`, `coding_agent`, `gnuradio_mcp`).

```yaml
coordinator:
  provider: env:AITHERNET_COORDINATOR_PROVIDER   # gemini_cli | openai_compatible | anthropic
  model: env:AITHERNET_COORDINATOR_MODEL         # blank -> the CLI/provider default model
  timeout_seconds: env:AITHERNET_COORDINATOR_TIMEOUT_SECONDS   # falls back to 60
  temperature: 0.2
  executable: env:AITHERNET_COORDINATOR_EXECUTABLE   # gemini_cli: falls back to "gemini"
  base_url: env:AITHERNET_COORDINATOR_BASE_URL       # openai_compatible / anthropic
  api_key: env:AITHERNET_COORDINATOR_API_KEY
```

Supported providers (pick one; `provider` and `executable` must match for CLI providers):

| `provider`          | reasons via                          | key config                          |
|---------------------|--------------------------------------|-------------------------------------|
| `gemini_cli`        | the authenticated **Gemini CLI**     | `executable: gemini` (+ optional model) |
| `openai_compatible` | an OpenAI `/chat/completions` HTTP API | `base_url`, `api_key`, `model`     |
| `anthropic`         | the Anthropic Messages API           | `api_key`, `model`                  |

#### Gemini CLI (`gemini_cli`)

Reasons via your authenticated Google-account **Gemini CLI** run headlessly. Install the
[Gemini CLI](https://github.com/google-gemini/gemini-cli) (it **requires Node.js 20+**),
log in once interactively (`gemini`), then select it:

```dotenv
AITHERNET_COORDINATOR_PROVIDER=gemini_cli
AITHERNET_COORDINATOR_EXECUTABLE=gemini
AITHERNET_COORDINATOR_MODEL=          # blank -> the authenticated CLI chooses its default model
AITHERNET_COORDINATOR_TIMEOUT_SECONDS=300
```

- **Reasoning only — Gemini never acts.** The coordinator runs `gemini --output-format json
  --skip-trust` with the prompt on **stdin**, in a dedicated empty sandbox directory under
  `$XDG_STATE_HOME/aithernet/coordinator/gemini-cli` — **outside the repository** (a repo
  cwd makes this CLI version send an invalid empty tools declaration → HTTP 400), and never
  the repo root, coding workspace, gr-mcp checkout, or $HOME itself. A
  workspace `.gemini/settings.json` denies built-in tools and MCP servers; headless mode
  defaults to *deny*; it is never run in YOLO/auto-approval mode. **Any reported tool call
  (`stats.tools.totalCalls > 0`) is treated as a provider failure** and the decision is
  rejected — Gemini does not edit files, run shells, call MCP, or fetch the web as the
  coordinator. GNU Radio operations still route through Aithernet's MCP runtime; Codex
  remains the coding agent.
- **Model selection:** leave `AITHERNET_COORDINATOR_MODEL` blank to delegate model choice
  to the authenticated CLI (reported back by the probe), or set a model to pass `--model`.
  What model your account actually serves is reported by the CLI probe — Aithernet does not
  assume any particular model for any subscription.
- **Readiness** is a cached, lightweight `gemini --version` identity probe — never a paid
  inference on status polls. Reasons: `executable_not_found`, `executable_not_runnable`
  (e.g. Node < 20), `executable_provider_mismatch`.
- **Authentication, OAuth caches, tokens, HOME, `.env` values, environment, and full
  prompts are never logged, persisted, returned by the API/events, or shown in the UI.**

```bash
aithernet coordinator status        # provider/executable/CLI version/readiness (no secrets)
aithernet coordinator diagnostics   # config-level checks (no inference)
aithernet coordinator diagnostics --probe   # one minimal authenticated headless inference
```

> Residual limitation: this Gemini CLI version's `--policy` deny-all rule makes the Gemini
> API reject the request (empty `tools[0].tool_type` → HTTP 400), so an explicit deny-all
> policy file is **not** used. Tool prevention is the empty sandbox + tool-denying settings
> + headless deny-by-default, and the authoritative guarantee is tool-call detection.

#### OpenAI-compatible (`openai_compatible`)

(OpenAI, vLLM, llama.cpp, Ollama's OpenAI shim, gateways) — set `provider:
openai_compatible` and:

```bash
export AITHERNET_COORDINATOR_MODEL="gpt-4o-mini"
export AITHERNET_COORDINATOR_BASE_URL="https://api.openai.com/v1"
export AITHERNET_COORDINATOR_API_KEY="sk-..."
```

`base_url` should be the API root (e.g. `.../v1`); the provider appends
`/chat/completions`. An API key is required unless `allow_unauthenticated: true` is set
for a local server. For **local Ollama**, set `base_url: http://127.0.0.1:11434/v1`, a
pulled `model` (e.g. `qwen2.5:7b`), and any non-empty `api_key` (e.g. `ollama`).

A failed reasoning call now returns an actionable, **secret-redacted** error — the
exception class, a non-empty description (even for empty-string httpx timeouts), the
sanitized endpoint, the HTTP status, and a bounded slice of the response body — e.g.
`[502] OpenAI-compatible request failed [ConnectError] at http://127.0.0.1:11434/v1/chat/completions: ...`
(never the empty `OpenAI-compatible request failed:` of before). If a server explicitly
rejects `response_format: {"type":"json_object"}`, the provider retries **once** without
that field; it never retries arbitrary failures.

**Anthropic** — set `provider: anthropic`, a `model` (e.g. `claude-sonnet-4-6`), and an
`api_key`; `base_url` defaults to `https://api.anthropic.com`. Required env vars are the
same three (`...MODEL`, optionally `...BASE_URL`, and `...API_KEY`).

Check readiness at any time without exposing secrets:

```bash
aithernet coordinator status
# or: curl http://127.0.0.1:8080/coordinator/status
```

### Coding agent provider (Stage 3)

The coding agent is configured under a `coding_agent:` block. Two **real** providers are
supported — pick one with `provider`; the `executable` **must match** it:

| `provider`     | real agent          | `executable` | identity probe (`--version` mentions) |
|----------------|---------------------|--------------|----------------------------------------|
| `claude_code`  | Claude Code CLI     | `claude`     | `claude`                               |
| `codex_cli`    | OpenAI Codex CLI    | `codex`      | `codex`                                |

```yaml
coding_agent:
  # provider + executable MUST match (see the table above).
  provider: env:AITHERNET_CODING_AGENT_PROVIDER       # claude_code | codex_cli; falls back to "claude_code"
  executable: env:AITHERNET_CODING_AGENT_EXECUTABLE   # falls back to "claude" (set "codex" for codex_cli)
  workspace: env:AITHERNET_CODING_AGENT_WORKSPACE     # falls back to "./workspace"
  timeout_seconds: 600
  output_format: text                                 # used by the claude_code provider
  extra_args: []
  # model: ...                 # optional model (passed as -m to codex)
  # env: {}                    # optional extra subprocess env (never reported by status)
```

**Provider/executable must match — readiness is verified.** Readiness is more than "the
executable resolves": each provider runs a short, cached **identity probe** (`<exe>
--version`) and verifies the binary actually behaves like the expected agent. So
`provider=claude_code` with `executable=codex` reports **not** configured (reason
`executable_provider_mismatch`) instead of running Codex with Claude's flags. Possible
readiness reasons: `executable` (not found), `executable_not_runnable`,
`executable_provider_mismatch`, `executable_probe_failed`. An explicit absolute path works;
no exact version string is required.

- **`workspace`** — the directory the agent runs in (created if missing). The agent works
  **only** inside this directory (Codex runs with `--sandbox workspace-write`), so it does
  not modify files outside the configured workspace.
- **`env`** — extra subprocess environment variables; **never** returned by the status
  endpoint.

Select Codex for this checkout (in `.env`, auto-loaded):

```dotenv
AITHERNET_CODING_AGENT_PROVIDER=codex_cli
AITHERNET_CODING_AGENT_EXECUTABLE=codex
AITHERNET_CODING_AGENT_WORKSPACE=$HOME/.local/state/aithernet/workspace
```

Then check status and submit a task:

```bash
aithernet coding-agent status
# provider: codex_cli   executable: codex   Configured: yes
aithernet coding-task submit "Create a file named CODEX_SMOKE_TEST.md describing this coding-agent smoke test."
```

The Codex provider invokes `codex exec` non-interactively, feeding the task prompt on
**stdin** (so a large prompt never goes on the argv), with `--sandbox workspace-write -C
<workspace> --skip-git-repo-check --json -o <final-message-file>` (and `-m <model>` when
configured). A non-zero exit is a persisted **failed** result; a timeout kills the process
cleanly. `files_changed`/`commands_run` are left empty unless Codex emits trustworthy
structured evidence — they are never invented.

### GNU Radio MCP server (Stage 4)

The node talks to an external GNU Radio MCP server over stdio. The intended/default server
is the ready-made [`yoelbassin/gr-mcp`](https://github.com/yoelbassin/gr-mcp) project — it
is **not** bundled here, so install it separately:

```bash
git clone https://github.com/yoelbassin/gr-mcp /absolute/path/to/gr-mcp
# follow gr-mcp's README to set it up (it is launched via `uv ... run main.py`)
```

The `gnuradio_mcp:` block mirrors gr-mcp's stdio configuration. `env:VAR_NAME` values
resolve from the environment; if `command` or any arg stays unset the server is reported
unconfigured and any tool call fails with a clear error (never faked GNU Radio behavior):

```yaml
gnuradio_mcp:
  provider: stdio
  command: env:AITHERNET_GNURADIO_MCP_COMMAND   # launches the server (e.g. gr-mcp's venv python)
  args:
    - "main.py"
  cwd: env:AITHERNET_GR_MCP_DIR                  # gr-mcp checkout (where main.py lives)
  timeout_seconds: 30
  stdio_stream_limit_bytes: 16777216            # 16 MiB; see "MCP stdio stream limit" below
  env: {}                                        # extra subprocess env (never reported)
```

This repository's local gr-mcp is launched with its own virtualenv Python and checkout
directory:

```dotenv
AITHERNET_GNURADIO_MCP_COMMAND=/path/to/gr-mcp/.venv/bin/python
AITHERNET_GR_MCP_DIR=/path/to/gr-mcp
```

(The classic `uv --directory <dir> run main.py` shape also works — set `command: uv` and
`args: ["--directory", "env:AITHERNET_GR_MCP_DIR", "run", "main.py"]`.) Then check status:

```bash
aithernet mcp status              # provider/command summary/readiness (no secrets)
aithernet mcp diagnostics --probe # launch the server + tools/list
aithernet mcp tools               # discover tools the server exposes (runtime tools/list)
aithernet mcp call <tool_name> --args-json '{"key": "value"}'
```

Tool names are discovered from the server at runtime — Aithernet does **not** hardcode any
gr-mcp tool names or GNU Radio workflows. The `env` map is never returned by `mcp status`.

**MCP stdio stream limit.** Large tool results (for example gr-mcp's full block catalog
from `get_all_available_blocks`) produce a single JSON-RPC frame bigger than asyncio's
64 KiB default line buffer, which previously crashed the backend with
`ValueError: Separator is found, but chunk is longer than limit` (an unhandled HTTP 500).
`stdio_stream_limit_bytes` (default **16 MiB**) raises that buffer; a frame that still
exceeds it now raises a clear `MCPClientError` and the API returns a **structured 502**
(the call is persisted as `failed`) instead of crashing. Increase the value if a server
legitimately returns even larger frames.

### Real gr-mcp integration (Stage 6)

**What gr-mcp is here.** [`yoelbassin/gr-mcp`](https://github.com/yoelbassin/gr-mcp) is an
*external* GNU Radio MCP server. In Aithernet's architecture it is the real tool interface
to GNU Radio. The production path is:

```
Aithernet node runtime
  → Aithernet stdio MCP client
    → external gr-mcp server (launched by your configured command)
      → GNU Radio
```

gr-mcp is **not** vendored, copied, or reimplemented in this repository, and Aithernet
never fakes GNU Radio behavior. You install and run gr-mcp yourself.

**1. Clone and set up gr-mcp (outside Aithernet), per its own README:**

```bash
git clone https://github.com/yoelbassin/gr-mcp /absolute/path/to/gr-mcp
cd /absolute/path/to/gr-mcp
# Follow gr-mcp's README to install it and its GNU Radio dependencies. It is launched
# in the stdio MCP shape:  uv --directory /absolute/path/to/gr-mcp run main.py
```

**2. Point Aithernet at it via the two documented environment variables:**

```bash
export AITHERNET_GNURADIO_MCP_COMMAND=uv
export AITHERNET_GR_MCP_DIR=/absolute/path/to/gr-mcp
```

These feed the `env:`-references in `configs/node.yaml`'s `gnuradio_mcp` block. If either
is unset, the server is reported **unconfigured** — never silently treated as connected.

**3. Verify the connection.** Diagnostics tell you whether Aithernet is truly wired to a
real gr-mcp/GNU Radio environment, without launching anything until you ask:

```bash
# Config-level checks only (does NOT launch gr-mcp):
aithernet mcp status
aithernet mcp diagnostics

# Actually launch gr-mcp and run tools/list through the real MCP client path:
aithernet mcp diagnostics --probe

# Enumerate the real tools the server exposes, then call one:
aithernet mcp tools
aithernet mcp call <tool_name> --args-json '{}'
```

> The **actual tool names come from `aithernet mcp tools`** (the server's runtime
> `tools/list`). Aithernet does not document or assume specific gr-mcp tool names — use
> whatever your installed gr-mcp version exposes.

**Or validate without starting the API server** — the standalone script uses the same
config loader and `MCPRuntime` as the node:

```bash
python scripts/validate_gr_mcp.py --config configs/node.yaml               # inspect config only
python scripts/validate_gr_mcp.py --config configs/node.yaml --list-tools  # launch + tools/list
python scripts/validate_gr_mcp.py --config configs/node.yaml \
    --call-tool <TOOL> --args-json '{"key": "value"}'                       # launch + call a tool
```

`aithernet mcp diagnostics` (and the script) report the provider, command summary,
whether the command resolves, unresolved args, cwd status, timeout, readiness, and
gr-mcp-oriented next steps — all **without leaking the `env` map or any secret**. A
`--probe` that fails returns a structured error (which capability/transport failed and
why), so you can tell a missing `uv`, an unset `AITHERNET_GR_MCP_DIR`, or a GNU-Radio
import error apart at a glance.

## Run the server

```bash
# Preferred: via the CLI (reads configs/node.yaml)
aithernet start
# Override bind address / config:
aithernet start --host 0.0.0.0 --port 9000 --config configs/node.yaml

# Or directly via uvicorn:
uvicorn aithernet.main:app --host 127.0.0.1 --port 8080
```

Interactive API docs are served at `http://127.0.0.1:8080/docs`.

### Endpoints

| Method | Path                          | Description                                       |
|--------|-------------------------------|---------------------------------------------------|
| GET    | `/health`                     | Service liveness.                                 |
| GET    | `/node/status`                | Node status + mission/event counts.               |
| GET    | `/coordinator/status`         | Coordinator provider/model/readiness (no secrets).|
| GET    | `/coordinator/diagnostics`    | Coordinator readiness; `?probe=true` runs one real inference. |
| GET    | `/coding-agent/status`        | Coding-agent provider/executable/workspace/readiness (no secrets). |
| POST   | `/missions`                   | Create a mission; emits `mission.received`.       |
| GET    | `/missions`                   | List missions, newest first.                      |
| GET    | `/missions/{id}`              | Get one mission (404 if absent).                  |
| POST   | `/missions/{id}/process`      | Run one coordinator reasoning step; returns a decision. |
| POST   | `/missions/{id}/step`         | Run one coordinator decision + routed execution step. |
| GET    | `/mission-steps`              | List mission steps; `?mission_id=` filter.        |
| GET    | `/mission-steps/{id}`         | Get one mission step (404 if absent).             |
| POST   | `/coding-tasks`               | Create a coding task (does not run it).           |
| GET    | `/coding-tasks`               | List coding tasks, newest first.                  |
| GET    | `/coding-tasks/{id}`          | Get one coding task (404 if absent).              |
| POST   | `/coding-tasks/{id}/run`      | Run a coding task; returns its result.            |
| POST   | `/coding-tasks/run`           | Create and immediately run a coding task.         |
| GET    | `/coding-tasks/{id}/result`   | Latest result for a task (404 if none).           |
| GET    | `/mcp/status`                 | MCP server provider/command/readiness (no secrets).|
| GET    | `/mcp/session`                | Managed MCP session status (Stage 11B; no secrets).|
| POST   | `/mcp/session/start`          | Start the managed session (idempotent).           |
| POST   | `/mcp/session/restart`        | Restart the session (new generation).             |
| POST   | `/mcp/session/stop`           | Stop the session and clean up the subprocess.     |
| GET    | `/gnuradio/context`           | Current GNU Radio workspace context (Stage 11B).  |
| POST   | `/gnuradio/context/refresh`   | Refresh context via available read-only tools.    |
| GET    | `/mcp/diagnostics`            | MCP diagnostic report; `?probe=true` uses the session. |
| GET    | `/mcp/tools`                  | List tools from the managed session; `?refresh=true`. |
| POST   | `/mcp/tools/{tool_name}/call` | Call an MCP tool; returns the audited call record.|
| GET    | `/mcp/tool-calls`             | List persisted MCP tool calls, newest first.      |
| GET    | `/mcp/tool-calls/{call_id}`   | Get one persisted MCP tool call (404 if absent).  |
| POST   | `/agents/connect`             | Connect an external agent; emits `external_agent.connected`. |
| GET    | `/agents`                     | List external agents; `?status=` filter.          |
| GET    | `/agents/status`              | External-agent roster (connected/total counts).   |
| GET    | `/agents/{id}`                | Get one external agent (404 if absent).           |
| PATCH  | `/agents/{id}/status`         | Set status (connected/disconnected/disabled).     |
| POST   | `/agents/{id}/disconnect`     | Mark the agent disconnected.                       |
| POST   | `/agents/{id}/disable`        | Mark the agent disabled.                           |
| POST   | `/agents/{id}/message`        | Record an inbound message; optional mission + one step. |
| GET    | `/agents/{id}/messages`       | List one agent's messages (404 if absent).        |
| GET    | `/agent-messages`             | List all agent messages; `?agent_id=`/`?mission_id=`. |
| GET    | `/events`                     | List events; `?mission_id=` filter.               |
| GET    | `/events/stream`              | Live event stream (Server-Sent Events).           |

`POST /missions/{id}/process`, `POST /missions/{id}/step`, the coding-task run endpoints,
and the MCP tool endpoints return **404** if the target is unknown, **503** if the relevant
agent/server is not configured, and **502** if the provider/server call fails. (A coding
task that runs but exits non-zero is a normal `200` response with a `failed` result. A
mission step whose route is blocked/failed is also a `200` response — the outcome is in the
step.)

Create a mission, then process it through the coordinator, with curl:

```bash
curl -X POST http://127.0.0.1:8080/missions \
  -H 'content-type: application/json' \
  -d '{"content": "Scan the 433 MHz band", "source_type": "user"}'

curl -X POST http://127.0.0.1:8080/missions/<mission_id>/process

# Or run one decision + routed execution step:
curl -X POST http://127.0.0.1:8080/missions/<mission_id>/step
```

### Decision routing contract

`POST /missions/{id}/step` asks the coordinator for one `CoordinatorDecision`, then routes
it. The coordinator sets `next_target` and a route-specific `structured_payload`:

| `next_target` (aliases)                         | Route        | Required `structured_payload`                                   |
|-------------------------------------------------|--------------|------------------------------------------------------------------|
| `respond`, `user`, `mission_source`             | response     | none (uses `message`/`summary`)                                  |
| `node_state`, `state`, `status`                 | node state   | none                                                             |
| `coding_agent`, `future_coding_agent`           | coding agent | `objective` (falls back to `message`), `context`, `available_tools`, `expected_outputs`, `reporting_requirements` |
| `gnuradio_mcp`, `mcp`, `future_gnuradio_mcp`    | MCP tool     | `tool_name` (required), `arguments`, `caller`                   |
| `continue_reasoning`                            | blocked      | — (autonomous loops are not enabled)                            |
| anything else (peer/manager/web-UI/unknown)     | blocked      | — (unsupported route)                                           |

Example coding-agent decision payload (`structured_payload`):

```json
{
  "objective": "Add an FM demodulator block to the flowgraph",
  "context": {"band": "96.5 MHz"},
  "available_tools": ["filesystem"],
  "expected_outputs": ["fm_demod.py"],
  "reporting_requirements": ["list files changed"]
}
```

Example GNU Radio MCP decision payload (`structured_payload`):

```json
{
  "tool_name": "create_flowgraph",
  "arguments": {"name": "rx_chain"},
  "caller": "coordinator"
}
```

A coding-agent/MCP route is **blocked** when that capability is not configured, **failed**
when the payload is malformed (e.g. missing `tool_name`), and **completed** when it runs —
the routed task/tool-call ids are linked in the step result for auditing.

Create and run a coding task (requires the coding agent to be configured):

```bash
curl -X POST http://127.0.0.1:8080/coding-tasks/run \
  -H 'content-type: application/json' \
  -d '{"objective": "Add a NOTES.md summarizing the workspace", "expected_outputs": ["NOTES.md"]}'
```

Connect an external agent and send it a message that creates a mission:

```bash
# Connect (endpoint_url is stored for later, never called automatically):
curl -X POST http://127.0.0.1:8080/agents/connect \
  -H 'content-type: application/json' \
  -d '{"name": "lab-orchestrator", "agent_type": "manager", "transport": "http",
       "endpoint_url": "https://lab.example/agent", "auth_type": "bearer"}'

# Send a message → creates a mission (source_type "external_agent"):
curl -X POST http://127.0.0.1:8080/agents/<agent_id>/message \
  -H 'content-type: application/json' \
  -d '{"content": "Scan the 433 MHz band", "message_type": "mission_request"}'

# Or create the mission AND run exactly one mission step (needs a configured coordinator):
curl -X POST http://127.0.0.1:8080/agents/<agent_id>/message \
  -H 'content-type: application/json' \
  -d '{"content": "Scan the 433 MHz band", "create_mission": true, "run_step": true}'
```

Tail the live event stream:

```bash
curl -N http://127.0.0.1:8080/events/stream
```

The stream is a *live tail* — it delivers events produced after you connect. Each
message is an SSE frame: an `event:` line with the event type and a `data:` line with
the JSON event. Use `GET /events` for history.

## Use the CLI

The CLI starts the server and otherwise acts as a client of a **running** node. If the
node is unreachable it prints a clear error rather than a stack trace.

```bash
aithernet start                              # start the server
aithernet status                             # print node status
aithernet coordinator status                 # print coordinator provider/readiness
aithernet coordinator diagnostics            # coordinator config checks (no inference)
aithernet coordinator diagnostics --probe    # one minimal authenticated inference
aithernet mission submit "Scan 433 MHz"      # submit a mission
aithernet mission submit "Scan 433 MHz" --process   # submit and process in one step
aithernet mission list                       # list missions
aithernet mission show <mission_id>          # show one mission
aithernet mission process <mission_id>       # run a coordinator step, print the decision
aithernet mission step <mission_id>          # run one decision + routed execution step
aithernet mission steps [--mission-id <id>]  # list mission steps
aithernet mission step-show <step_id>        # show one mission step
aithernet coding-agent status                # print coding-agent provider/readiness
aithernet coding-task create "Add a filter"  # create a coding task (don't run it)
aithernet coding-task list                   # list coding tasks
aithernet coding-task show <task_id>         # show one coding task
aithernet coding-task run <task_id>          # run an existing coding task
aithernet coding-task submit "Add a filter"  # create and immediately run a coding task
aithernet mcp status                         # print MCP server provider/readiness
aithernet mcp diagnostics                    # MCP diagnostic report (no server launch)
aithernet mcp diagnostics --probe            # launch the server + run tools/list
aithernet mcp tools                          # list tools the MCP server exposes
aithernet mcp call <tool> --args-json '{}'   # call a tool (optional --mission-id/--task-id/--caller)
aithernet mcp calls                          # list persisted MCP tool calls
aithernet mcp call-show <call_id>            # show one persisted MCP tool call
aithernet mcp session status                 # persistent MCP session state (Stage 11B)
aithernet mcp session start|restart|stop     # manage the long-lived session
aithernet gnuradio context                   # show GNU Radio workspace context
aithernet gnuradio context refresh           # refresh context (read-only tools only)
aithernet agents connect --name NAME         # connect an external agent (--type/--url/--transport/--auth-type)
aithernet agents list                        # list external agents
aithernet agents show <agent_id>             # show one external agent
aithernet agents status                      # external-agent roster summary
aithernet agents set-status <id> <status>    # connected | disconnected | disabled
aithernet agents message <id> "do X"         # send a message (--no-create-mission/--run-step/--payload-json)
aithernet agents messages [--agent-id <id>]  # list agent messages (also --mission-id)
aithernet events list                        # list events
aithernet events list --mission-id <id>      # events for one mission
aithernet events stream                      # live-tail events (Ctrl-C to stop)
```

`coding-task create`/`submit` accept `--mission-id`, `--context-json '<json object>'`, and
repeatable `--available-tool`, `--expected-output`, and `--reporting-requirement` options.

All client commands accept `--url http://host:port` to target a specific node and
`--config path/to.yaml` to resolve the URL from a config file. If an agent is not
configured or a provider call fails, the CLI prints a clean error.

## Run the web dashboard

The Stage 8 dashboard (under [`web/`](web/)) is a Vite + React + TypeScript SPA that drives
the **running** node through its API. It needs Node.js 18+ and npm.

**1. Start the backend** (it enables CORS so the browser can call it):

```bash
aithernet start                 # serves the API on http://127.0.0.1:8080
```

**2. Start the dashboard dev server:**

```bash
cd web
npm install
npm run dev                     # Vite dev server, typically http://localhost:5173
```

Open the URL Vite prints. By default the UI calls `http://127.0.0.1:8080`. To point it at a
different node, set the API base URL before `npm run dev` (or `npm run build`):

```bash
export VITE_AITHERNET_API_BASE_URL="http://127.0.0.1:9000"
# or copy web/.env.example to web/.env and edit it
```

**Build a static bundle** (optional). If you build it, the backend automatically serves it
at `http://127.0.0.1:8080/dashboard`:

```bash
cd web
npm run build                   # outputs web/dist (typechecks first)
npm run lint                    # eslint (optional)
```

> When served from `/dashboard`, the dashboard still calls the API at
> `VITE_AITHERNET_API_BASE_URL` baked in at build time (default `http://127.0.0.1:8080`).

**Panels:** Node status · Mission Composer (*Submit* / *Submit + Run Step*) · Live Activity
(SSE event stream + *Refresh recent*) · Missions & one-step execution · External Agents
(connect / status / message) · GNU Radio MCP (status / diagnostics / probe / tools / call /
persisted calls) · Coordinator & Coding-Agent status + coding tasks.

**Manual validation** (with the backend running and `npm run dev` open):

1. Start the backend: `aithernet start`.
2. Start the UI: `cd web && npm install && npm run dev`; open the Vite URL.
3. **Submit a mission** in the Mission Composer; it appears in Missions and the event stream.
4. Select the mission and **Run one step**; the routed step/decision is shown (no loop).
5. **Connect an external agent** in the Agent panel.
6. **Send the agent a message** (optionally `create_mission` / `run_step`); the mission /
   step result is shown.
7. In the MCP panel, **Diagnostics** then **Probe** (probe launches the configured gr-mcp
   server) and **List tools** → **Call tool**. With MCP unconfigured you get the real
   status/error, never fabricated tools.

**Error labeling & large results.** The dashboard categorizes failures — `network`,
`timeout`, `http`, `parse` — so only a real connection failure shows **backend
unavailable**; an HTTP 500/502/400 shows the backend's actual `detail` (e.g.
`[502] OpenAI-compatible request failed [ConnectError] …` or `[502] MCP response exceeded
the configured stdio stream limit …`), never mislabeled as an outage. The MCP panel renders
large tool results in a scrollable, bounded preview with a truncation notice — the full
result is still persisted on the node (`GET /mcp/tool-calls`), never truncated server-side.
The coding-agent panel shows the real provider (e.g. `codex_cli`) and the readiness reason
when not configured. Frontend wrapper tests run with `cd web && npm run test`.

If the backend is not running, the header shows **backend unavailable** with the error, and
every panel surfaces the real failure rather than placeholder data.

## Run the tests

```bash
pytest
```

Run linting too (scripts are included):

```bash
ruff check src tests scripts
```

Tests run against an isolated temporary SQLite database per test, so they never touch a
developer's local node state. Coordinator, coding-agent, MCP, mission-step, diagnostics,
and external-agent tests inject deterministic provider/client doubles (including a scripted
coordinator that returns a fixed decision), so **no external API key, no Claude Code
install, and no gr-mcp / GNU Radio install are required** to run the suite. The
external-agent tests make **no outbound network calls** — agents are message sources and
the node never calls `endpoint_url`. The Stage 8 dashboard tests cover only the backend
additions (CORS headers and the optional `/dashboard` static mount) and **never build or
require the frontend**; building `web/` is a separate, optional Node/npm workflow. The stdio MCP client and the MCP
diagnostics (including the `--probe` path and `scripts/validate_gr_mcp.py`) are exercised
against a tiny MCP server written to a pytest temp path (real JSON-RPC subprocess I/O,
including the exact `uv --directory … run main.py` command shape, plus a marker-file check
proving a non-probe diagnostic never launches the server).

## Multi-RF backends (Stage 13A.5)

Aithernet operates one or more **external RF backends**, each as its own MCP **subprocess**.
The boundary is strict:

> **Marconi does not replace the Aithernet mission runtime.**
> **Aithernet does not reimplement Marconi's RF engine.**
> **Aithernet operates RF backends through audited atomic MCP calls.**

* **`legacy_gr_mcp` — stable default.** The low-level GNU Radio MCP server
  (`yoelbassin/gr-mcp`); block/connection/flowgraph operations. Autostarts; backs the
  existing `gnuradio_mcp` route, `/mcp/*` APIs, `aithernet mcp …` CLI, and GNU Radio context
  — all unchanged.
* **`marconi` — experimental, opt-in.** The higher-level Marconi MCP server
  (captures / PSD / signals / measurements / scenes / pipelines / runs / plots / `.grc`
  export, all **simulation / file-analysis** in this stage — **no live hardware**). It is
  **non-autostart**; start it explicitly.

**External-process integration only.** Aithernet talks to each backend purely over its MCP
stdio subprocess. It **never imports** Marconi's (or gr-mcp's) Python modules, **never
vendors** GPL source, and **never runs Marconi's Claude Code plugin or skills** — the plugin
is not a second coordinator. **Tool names and schemas are discovered live** from each server;
nothing is hardcoded. Backend selection is **model-driven** (the coordinator names the backend
and tool in an `rf_mcp` step) — there is **no keyword routing** and no automatic preference.
Each mission iteration performs **at most one** MCP tool call; failed calls are never replayed.

**Backend-specific contexts.** The legacy backend keeps its existing GNU Radio context; other
backends get a generic per-backend RF workspace context (devices/captures/signals/…/artifacts
summaries). A restart marks a backend's context **stale** until refreshed.

**Artifact workspace containment.** Marconi writes artifacts beneath its configured workspace.
Aithernet stores only **workspace-relative references** (never the bytes, never absolute
paths); traversal and symlink escapes are rejected, and unrelated files are never exposed.

Configure Marconi (leave unset to keep it disabled — the legacy backend stays the default):

```bash
AITHERNET_MARCONI_COMMAND=/path/to/marconi/.venv/bin/marconi-mcp
AITHERNET_MARCONI_DIR=/path/to/marconi
AITHERNET_MARCONI_WORKSPACE=$HOME/.local/state/aithernet/rf/marconi
```

```bash
aithernet rf backends                       # list every backend + state/version/tools
aithernet rf backend start marconi          # start the experimental backend (opt-in)
aithernet rf tools marconi                  # live-discovered tool catalog
aithernet rf call marconi list_devices      # one atomic read-only tool call
aithernet rf context marconi                # backend RF workspace context
aithernet rf artifacts --backend-id marconi # indexed workspace-relative artifacts
aithernet rf benchmark run tool_discovery   # operator benchmark on every backend that fits
```

**Benchmark methodology.** `aithernet rf benchmark` records **factual** metrics
(backend + version/revision, success, MCP calls, elapsed time, validation outcome, artifact
count/types, repeatability). Each scenario step is one atomic MCP call (no hidden multi-tool
step). It **never changes the default backend** and **never declares a winner** across
non-equivalent scenarios.

**Pinning / upgrading / rollback.** Marconi's revision is external and operator-pinned
(recorded in the operator-maintained `marconi-pinned-commit.txt`). To upgrade safely: update the
external Marconi checkout in its own venv, re-run its test suite there, then point
`AITHERNET_MARCONI_COMMAND` at the new `marconi-mcp`. To roll back, simply unset the Marconi
variables (or `aithernet rf backend stop marconi`) — the stable `legacy_gr_mcp` backend
remains the default and is unaffected.

**Current limitation.** Marconi here is simulation/file-analysis only; live radio
transmission is out of scope.

## Coordinator peer messaging & durable reply resumption (Stage 13B)

Stage 13B lets the **coordinator** decide — mid-mission — that talking to a trusted peer node
is useful, send **exactly one** durable message, optionally place the mission into a **durable
reply wait**, and resume reasoning when an **authenticated, correlated reply** arrives. The
reverse direction is symmetric: a trusted remote node may submit a coordinator-addressed
request that becomes a local mission, and the local coordinator sends one correlated response
that resumes the remote node's waiting mission.

This builds **on top of** the Stage 13A signed agent transport (Ed25519 envelopes, inbox /
outbox, delivery worker, ACKs). The application payload travels **inside** an ordinary signed
`agent_message` envelope — no new envelope kind — so Stage 13A's transport guarantees are
preserved unchanged.

**Two distinct trust concepts (do not conflate).**

* **Transport trust** is *identity*: a peer's Ed25519 public key is pinned (`/peers/{id}/trust`).
  This only proves *who* signed a message.
* **Application authorization** is *permission*: separate per-peer flags govern what an
  *authenticated* peer is allowed to do at the coordinator-message layer. Defaults are
  conservative — a peer you trust for transport is **not** automatically allowed to open
  coordinator requests.

  | Permission | Default | Meaning |
  |---|---|---|
  | `may_send_requests` | **false** | peer may open coordinator-addressed *requests* (→ inbound missions) |
  | `may_send_replies` | true | peer may send *replies* that satisfy your reply waits |
  | `may_receive_messages` | true | you may send coordinator messages to this peer |
  | `may_request_response` | **false** | peer may demand a response (creates a response obligation) |
  | `max_inbound_request_bytes` | config | per-request payload ceiling |
  | `max_concurrent_inbound_missions` | config | open inbound-request missions per peer |

  View/update via `GET`/`PATCH /peers/{peer_id}/permissions` or
  `aithernet peer permissions show|set`. Permission changes never alter key trust, and trust
  changes never alter permissions.

**The autonomous loop.**

```
mission active
  → coordinator chooses a `peer_message` step (at most ONE message per iteration)
  → Aithernet queues exactly one durable outbound message (Stage 13A delivers + ACKs it)
  → coordinator MAY place the mission into a durable reply wait (a separate decision)
  → an authenticated, correlated reply arrives
  → the reply satisfies the wait EXACTLY ONCE → mission requeued
  → coordinator reassesses using the reply
```

**Invariants enforced.**

* **One message per step.** A `peer_message` decision queues a single outbound message;
  endpoints, keys, signatures and retry knobs in the decision are ignored.
* **ACK ≠ answer.** A transport delivery ACK marks the message *delivered*, never *replied to*.
  Reply waits are satisfied only by a correlated semantic reply (`reply_to_request_id`), and
  the mission communications context surfaces `transport_acknowledged` and `semantic_reply`
  separately.
* **Exactly-once resume.** Wait satisfaction and resume use atomic compare-and-set on the wait
  record, so a reply (even a duplicate, even one racing the wait creation) resumes the mission
  at most once. Reply-before-wait, reply-after-wait and reply-during-transition are all handled.
* **Correlation.** Replies are matched to the originating request by `request_id`; an
  uncorrelated, unauthorized, or wrong-peer reply is recorded and **never** satisfies a wait.
* **Timeouts.** A reply wait has a deadline (`default_reply_deadline_seconds`, capped by
  `max_reply_deadline_seconds`); the mission worker sweeps overdue waits, times them out, and
  requeues the mission so the coordinator can react — **without** resending the original message.
* **Transport retries stay transport retries** — they never create new mission steps or repeat
  coordinator actions.
* **Inbound requests.** A trusted+authorized peer's request is schema-validated and bounded,
  then becomes a durable inbound mission. The untrusted remote text is **delimited and labeled**
  in the mission prompt — never spliced into the system prompt, never executed as instructions.
  Duplicate `request_id`s yield one mission and one inbox record.
* **Response obligation.** When a peer is authorized to *require* a response, the inbound
  mission cannot *complete* until a reply has actually been queued; until then it is held
  (blocked control step), not silently finished.
* **No leakage.** Comms APIs/CLI/dashboard never expose private keys, signatures, raw
  envelopes, prompts, credentials, or environment.
* **Restart recovery.** Pending waits, undelivered messages, and open inbound requests survive
  a restart (`runtime.comms.recover()` on startup); nothing is double-sent or double-resumed.

**APIs.**

```
GET   /missions/{id}/communications              # outbound msgs, reply waits, response obligation
GET   /missions/{id}/reply-waits                 # durable reply waits for a mission
POST  /missions/{id}/reply-waits/{wait_id}/cancel
GET   /conversations[/{id}[/messages]]           # correlated message threads
GET   /inbound-requests[/{request_id}]           # peer coordinator requests + disposition
GET   /peers/{peer_id}/permissions               # application authorization (not key trust)
PATCH /peers/{peer_id}/permissions
```

**CLI.**

```bash
aithernet mission communications MISSION_ID        # outbound + waits + obligation
aithernet mission reply-waits MISSION_ID
aithernet reply-wait cancel WAIT_ID
aithernet conversation list | show CONVERSATION_ID
aithernet inbound-request list | show REQUEST_ID
aithernet peer permissions show PEER_ID
aithernet peer permissions set PEER_ID --may-send-requests   # authorize inbound requests
```

**Two-node setup (sketch).** Each node initializes its identity, registers the other as a peer,
**pins its key** (transport trust), and — separately — **grants permissions** for the
coordinator layer:

```bash
# on node B, allow trusted node A to open coordinator requests
aithernet peer permissions set <A-peer-id> --may-send-requests --may-request-response
```

Enable/tune the layer under `communication:` in node config (see `.env.example`).

**Out of scope (delivered in Stage 13C, below).** Stage 13B shipped only a minimal read-only
communications panel; the full operator dashboard is Stage 13C.

## Operator dashboard (Stage 13C)

Stage 13C makes the existing distributed system **observable and operable** without redesigning
the mission engine, transport, RF registry, or peer-message correlation. It exposes the real
persisted state — it never fabricates “online”, “delivered”, “answered”, “completed”, or
“healthy”.

**Navigation.** The dashboard ([web/](web/)) is organized into top-level areas:

| Area | Shows |
|---|---|
| **Overview** | node identity + fingerprint, worker/coordinator/coding-agent/RF health (from live state, not a flag), peer/outbox/inbox counts, active/waiting missions, pending reply waits, inbound requests, recent failures |
| **Missions** | mission list/execution/steps **plus** a distributed-communication timeline + topology |
| **Conversations** | a filterable conversation list and a per-conversation timeline + composer |
| **Fleet** | peer list/detail with bounded operator actions |
| **RF Backends** | the existing RF/MCP/GNU-Radio panels |
| **Diagnostics** | outbox/inbox, reply waits, inbound requests, event stream |

**Trust vs. permissions.** The Fleet view renders **public-key trust** (identity) and
**application authorization** (per-peer permissions) as *separate* indicators, and edits them
separately — granting `may_send_requests` never changes key trust, and trusting a key never
grants authorization. Full public keys are never shown (only an abbreviated `sha256:` fingerprint).

**Conversations.** The list filters by peer, direction, pending-reply, and id; the detail
timeline visually distinguishes request/update/reply, inbound/outbound, transport **ACK** vs.
**semantic reply**, and the reply-wait lifecycle. Message content is a bounded, escaped preview —
never a raw envelope, signature, key, or header. An expandable “metadata” block shows sanitized
ids only.

**ACK vs. semantic reply.** Everywhere — UI, CLI, and the aggregate APIs — a transport ACK
(`ACK ✓ (delivered, not answered)`) is shown distinctly from an authenticated, correlated
**semantic reply** that satisfies a wait. Queued/acknowledged never implies answered.

**Distributed mission timeline.** Mission detail shows the peer_message action, durable wait,
semantic reply, timeout, resume, inbound-request source, and response obligation, plus a
lightweight node-and-edge **topology derived only from persisted correlation records**. Remote
mission status is shown as **unknown** — the dashboard never invents what a peer did not supply.

**Operator composer.** Inside a peer/conversation view, the operator may queue **one** message
to a trusted, enabled, authorized peer. There is no field for an endpoint, signing identity,
HTTP header, or retry policy — the node signs and delivers through the existing durable Stage 13A
outbox (the browser never contacts a peer). The message is persisted before delivery, never
creates a mission locally, and is distinguishable from coordinator-generated messages (it carries
no `mission_id`). A double-click cannot queue twice.

**Reply-wait & delivery operations.** Pending/satisfied/timed-out/cancelled reply waits are
listed; an operator may **cancel** a pending wait (with confirmation) but can never mark one
satisfied or fabricate a reply. The outbox view allows confirmed **retry** of failed/dead-letter
messages and **cancel** of pending ones — transport retries reuse the same message id and never
create a new mission step.

**Inbound requests.** The inbound view makes explicit that *transport authenticated does not
necessarily mean application authorized* — an authenticated-but-unauthorized request creates no
mission. Rejected remote payloads are shown only as a bounded sanitized preview.

**RF evidence linkage.** RF actions are linked by backend id / version / tool-call / artifact
**metadata only** — no artifacts are transferred between nodes and no arbitrary download paths
are exposed.

**Read-only & bounded APIs** (additive — existing APIs unchanged):

```
GET  /overview                              # node + worker + fleet + comms health rollup
GET  /fleet                                 # per-peer trust/permissions/health/linkage
GET  /conversations/{id}/timeline           # ordered, bounded conversation timeline
GET  /missions/{id}/distributed-timeline    # distributed correlation timeline + topology
GET  /communications/summary                # fleet-wide outbox/inbox/waits/inbound rollup
GET  /reply-waits[?state=]                  # all reply waits, filterable
POST /communications/send                   # operator composer (the only mutation here)
```

Aggregate endpoints are read-only, paginated/bounded, sanitized, typed, and avoid N+1 queries
(per-peer aggregation is done in memory over a single bounded fetch).

**CLI parity.**

```bash
aithernet overview
aithernet fleet list | show <peer-id>
aithernet conversation list | show <id> | timeline <id> | send <peer-id> --text "…"
aithernet communication outbox | inbox | waits | summary
aithernet mission distributed-timeline <mission-id>
```

**Real-time & accessibility.** Reads use bounded polling / the existing event stream and create
**no** side effects (no send/resume from a read or a poll); stale results are discarded on
navigation. Controls are keyboard-operable with labels and visible focus; status is never
conveyed by colour alone (every badge pairs a colour with text); destructive actions require an
accessible inline confirmation; ids are truncated with a copy-to-clipboard for the full safe id.

**Security boundaries.** The frontend never receives private keys, signatures, raw envelopes,
subprocess environments, or model prompts; remote structured data is rendered as escaped text
(no HTML/script injection); the composer cannot pick an endpoint, identity, or header, or execute
code; conversation reads and polling cause no transport or mission side effects.

**Two-node UI setup.** Run two nodes with separate ids/identities/databases/ports/dirs/RF
workspaces (as in the Stage 13B two-node setup), point each browser’s
`VITE_AITHERNET_API_BASE_URL` at the respective node, trust + authorize the peer in the Fleet
view, then use the composer and watch the conversation/distributed-mission timelines on both
sides.

## Distributed evidence & canonical conversations (Stage 13D, in progress)

Stage 13D closes the Stage 13C limitations. Two foundations are implemented and tested; the
remaining transfer wiring is described under *Status* below.

**Canonical conversations (delivered).** A reply that omits `conversation_id` no longer starts a
second conversation row: it inherits the original request/reply chain by **provable correlation
only** — never by text similarity, timestamp proximity, keywords, or model reasoning. Resolution
precedence is explicit: an existing valid `conversation_id` → this node's original outbound
request → the inbound request message → the reply wait → the durable inbound-request record → a
freshly generated id. Correlation is restricted to the *expected authenticated peer*, so a
request-id collision from a different peer never merges conversations. Resolution runs on both
the send path (the omitted id is filled before the envelope is signed) and the inbound path (a
stored reply is rethreaded onto the canonical conversation — only the denormalized grouping
column moves; the signed envelope is never mutated). The conversation list, timeline,
communication summary, and distributed topology therefore all use one canonical identity.
A bounded, idempotent, non-destructive historical repair is available:

```bash
aithernet conversation repair --dry-run   # report provable split chains, change nothing
aithernet conversation repair --apply      # rethread them (no message is deleted)
```

The repair only touches provable splits, leaves ambiguous rows unchanged, preserves every
message id and link, and is idempotent (re-running finds nothing).

**Managed content-addressed artifact store (delivered).** Binary RF-artifact bytes live ONLY on
disk in a managed store below the configured `artifacts.state_directory` — never in SQLite,
events, prompts, or dashboard JSON. Layout: `objects/sha256/<aa>/sha256_<digest>` (final,
content-addressed, read-only), `partial/<transfer_id>` (in-progress receive buffers),
`quarantine/` (corrupt/mismatched bytes, never served), `manifests/`. The store streams hashing
(never loading whole captures into memory), deduplicates identical content, commits a final
object by **atomic rename only after the bytes match the expected SHA-256 and byte size**,
rejects path traversal / symlink escape / device / FIFO / socket sources, enforces per-artifact,
total-store, per-peer, and minimum-free-disk limits, and never garbage-collects a pinned object.
A digest or size mismatch quarantines the bytes and never publishes the object; the original
backend-workspace file is left where the backend created it. Tuned under `artifacts:` in node
config (see `.env.example`).

**Authenticated cross-node artifact transfer (delivered — Stage 13D.2).** The full flow is now
implemented and tested: a node **offers** a stored artifact or a peer **requests** one; a signed
`artifact_offer`/`artifact_request`/`artifact_grant`/`artifact_reject`/`artifact_complete`/
`artifact_failed` control protocol (carried inside the existing signed Stage 13A envelope, bounded
metadata only — never bytes/URLs/paths) authorizes the exact transfer; the **grant binds**
transfer id + digest + size + sender + receiver + expiry, so a grant for one peer/artifact can
never be reused by another. The receiving node's background **transfer worker** (independent of
the mission and RF workers) streams validated byte ranges from the sender's authenticated
`POST /agent/v1/artifact` endpoint into the managed store's partial buffer, **resumes** from the
persisted offset after an interruption/restart, verifies the **exact SHA-256 + byte size**, and
**atomically imports** the object — linking it to the origin peer, mission, and conversation. A
digest/size mismatch quarantines and never publishes; cancelled transfers never resume; expired
grants are unusable; duplicate requests/chunks are idempotent. Per-peer artifact permissions
(`may_offer_artifacts`/`may_request_artifacts`/`may_receive_artifacts` + size/concurrency/kind
limits) are **separate from transport trust** and default off. A `peer_artifact` coordinator
route performs exactly one offer/request/cancel action (and one MissionStep) per iteration — it
cannot choose a URL, path, digest, destination, range, header, or signing identity. APIs
(`/artifacts`, `/artifact-transfers`, `/artifact-transfers/offer|request|{id}/cancel|retry`,
`/artifact-store/status|gc`, `/peers/{id}/artifact-permissions`), CLI (`aithernet artifact …`),
and an **Artifacts** dashboard view expose only sanitized metadata + bounded controls; the
browser never contacts a peer's binary endpoint.

```bash
aithernet artifact list | show <id>
aithernet artifact offer <artifact-id> <peer-id>
aithernet artifact request <remote-artifact-id> <peer-id>
aithernet artifact transfer list | show <id> | cancel <id> | retry <id>
aithernet artifact store status | gc --dry-run | gc --apply
```

**Authenticated remote mission-status synchronization (delivered — Stage 13D.3).** When a node
runs an inbound mission a peer requested, deterministic infrastructure reports its OWN mission
lifecycle back to that peer over the SAME signed durable outbox — no second transport or mission
engine. Each committed transition (received → queued → active → waiting → … → completed/failed/
cancelled) queues one bounded, signed `aithernet.mission-status` update carrying only factual
fields (origin node, remote mission ref, request/conversation correlation, state, an
infrastructure-allocated **monotonic sequence**, timestamps, a bounded progress summary, terminal
category, and artifact/transfer counts) — never prompts, reasoning, tool internals, paths,
signatures, keys, or bytes. Status publication creates **no MissionStep**; a transport retry
creates none either; and a failed delivery never rolls back the mission transition.

The receiver authenticates the sender, requires explicit per-peer permission (separate from trust
**and** from message/artifact permissions — all default false), verifies the payload origin is
the sender's own node, correlates to the local initiating mission by request id, and keeps **one
ordered snapshot** per remote relationship. Updates are strictly ordered: a newer sequence is
accepted; a duplicate is idempotent; an **older sequence is ignored**; a **same-sequence message
with a different payload is rejected** and recorded. A remote update **never** mutates a local
mission, never satisfies a reply wait, and the wrong peer can never modify a snapshot. Every
received message is recorded append-only with its disposition (`accepted` / `duplicate` /
`older_sequence_ignored` / `invalid_transition_rejected` / `unauthorized_rejected` /
`unknown_mission_rejected` / `malformed_rejected`).

Freshness is explicit and computed from persisted timestamps: **unknown** (no snapshot),
**fresh** (recent non-terminal), **stale** (non-terminal past the configurable
`status_freshness_seconds` deadline), **terminal** (a terminal state). Stale is never converted
to failed/completed by inference, and terminal is never inferred from staleness. An authorized
node may **query** a known snapshot; the peer answers from persisted local fact (no enumeration,
no arbitrary target/sequence/state from the coordinator), refreshing freshness. Unsent updates
ride the durable outbox and survive a restart; an old outbox retry can never replace newer
accepted state. The **distributed topology** now links the full chain from persisted facts only:
initiating mission → peer request → remote mission (with authenticated state + freshness) →
status sync → reply wait → reply → artifact transfer → imported artifact.

```bash
aithernet remote-mission list | show <snapshot-id> | events <snapshot-id> | query <snapshot-id>
aithernet mission status-publications <mission-id>
aithernet peer mission-status-permissions show <peer-id>
aithernet peer mission-status-permissions set <peer-id> --may-publish --may-query --may-receive
```

APIs (`GET /remote-missions[/{id}[/events]]`, `POST /remote-missions/{id}/query`,
`GET /missions/{id}/status-publications`, `GET|PATCH /peers/{id}/mission-status-permissions`) and
a **Remote Missions** dashboard view expose only sanitized, bounded data with explicit
unknown/fresh/stale/terminal labels. Tuned under `communication:` in node config (see
`.env.example`).

**Status.** All four Stage 13C limitations are now closed and tested: conversation splitting
(13D.1), metadata-only artifacts (13D.2), and **remote mission status + distributed topology
(13D.3)**. There is no remaining limitation corresponding to authenticated remote mission-state
synchronization or a topology lacking remote-status / artifact-transfer edges.

## Production deployment & operations (Stage 14A)

Stage 14A makes the completed node safely installable, runnable, upgradeable, recoverable,
observable and supportable outside the development checkout — without redesigning the mission
engine, coordinator, transport, RF backends, or distributed protocols. Full operator guide:
[deploy/README.md](deploy/README.md). Highlights:

* **Production layout** — all persistent state lives beneath an explicit `node_state_root`
  (config/db/identity/run/logs/backups/rf/coordinator/coding/artifacts/tmp); production never
  depends on the repo working directory.
* **Provision + preflight** — `aithernet node provision <root> --node-id … --node-name …`
  (idempotent, `--dry-run`, never overwrites an existing identity/database) and
  `aithernet node preflight` (categorized, sanitized config/env validation).
* **Readiness vs liveness** — `GET /health/live` (cheap) and `GET /health/ready` (200 only after
  migrations, repositories, and the required workers/RF backend initialize; 503 otherwise). An
  experimental non-autostart backend never makes the node unready.
* **Graceful shutdown** — ordered stop on SIGINT (workers → transport → RF → subprocesses); no
  orphan MCP/coordinator/coding processes; pending outbox + waits stay durable; missions recover.
* **Backup / restore** — consistent SQLite online-backup + manifest + per-file SHA-256;
  identity-merge-protected, checksum-verified, rollback-snapshot-preserving restore.
* **Upgrade / rollback** — backup → migrate → health-check, stop-on-failure, explicit rollback;
  external RF backend revisions are recorded but never modified; no self-update from the network.
* **Structured logging** — JSON lines with a redaction filter (no keys/signatures/envelopes/
  prompts/credentials/env); journald + optional rotating file.
* **Diagnostics bundle** — `aithernet diagnostics bundle` writes a sanitized support archive (no
  db contents, private identity, secrets, or RF captures).
* **systemd** — `deploy/systemd/aithernet.service` (or `aithernet node systemd-unit`): bounded
  restart backoff, graceful stop timeout, subprocess-tree reaping, EnvironmentFile secrets.
* **Dashboard** — a read-only **Operations** section (version, migrations, readiness/components,
  storage, backup freshness, preflight). Destructive restore/upgrade are CLI-only.

## Managed SDR hardware inventory & leasing (Stage 14B)

Stage 14B adds hardware **ownership and lifecycle** on top of the existing RF backends:
`discovery → stable device record → factual capability probing → health/availability →
compatibility check → durable lease → backend binding → active RF work → renewal →
release/recovery`. It adds no RF sample processing itself, embeds no vendor logic in the mission
engine, adds no second RF backend registry, and never modifies the external RF backends. It is
**off by default** (`hardware.enabled: false`) — a node with no SDR hardware stays fully usable for
simulation, coding, coordination, and artifact analysis.

* **Discovery providers** — a generic `HardwareDiscoveryBackend` interface with `soapy`
  (`SoapySDRUtil --find`), `uhd` (`uhd_find_devices`), `static`/`static_file` (operator-declared
  descriptors), and a generic `command` adapter. Production code **never invents devices**; an
  absent tool reports unavailable instead of crashing startup. Tests inject an explicit fake.
* **Stable identity** — a deterministic `hardware_key` from `provider + driver + vendor + product +
  serial + channel` survives restart and USB re-enumeration; a serial-less device is marked
  identity-limited; a disappeared device is kept as history and rediscovery updates it in place.
* **Factual capabilities** — RX/TX, duplex, channels, frequency/sample-rate/gain/bandwidth ranges,
  antennas, clock/time sources, stream formats, driver args — source-attributed; **unknown stays
  unknown** (never inferred from a model name).
* **Durable leases** — atomic, conflict-checked grant (no two conflicting exclusive leases; **never
  a shared transmit**; shared receive only when the device declares it safe), bounded duration,
  renewal, explicit/cancellation/terminal-mission release, expiry, operator revocation, and
  restart/orphan/device-disappearance reconciliation. Renewal and polling create **no MissionStep**;
  `lease_token`/owner are never exposed.
* **Coordinator integration** — one atomic `rf_device` route (acquire/release/refresh). The
  coordinator selects only a known `device_id` + `backend_id` + bounded purpose + bounded RF
  requirements — never a device string, driver argument, shell fragment, path, or environment. A
  deterministic binding adapter derives the backend device-args from the persisted device plus an
  administrator-approved binding.
* **Surfaces** — `aithernet hardware …` CLI, typed `/hardware/*` API (read endpoints always
  available; lease/admin actions gated by `operator_lease_actions_enabled`), and a read-only
  **Hardware** dashboard tab (providers, devices, leases — accessible text states, revoke with
  confirmation). Preflight/readiness/diagnostics/backups all include managed hardware.

```bash
aithernet hardware providers
aithernet hardware refresh
aithernet hardware list
aithernet hardware lease acquire <device-id> --backend-id legacy_gr_mcp --direction rx
aithernet hardware lease revoke <lease-id>     # confirmation required
```

See `deploy/README.md` §12 for the full `hardware` config block.

## Real SDR hardware qualification (Stage 14C.1)

Stage 14C.1 hardens the managed-hardware runtime against ACTUAL attached SDR hardware. The
production `soapy`/`uhd` discovery providers now run a deep per-device probe to record factual
capabilities (frequency/sample-rate/gain/antenna/channel/format/timestamps — unknown stays
unknown), with on-demand probing that never disturbs a leased device. RF flowgraph execution runs
in a **separately configured** GNU Radio interpreter (`hardware.qualification.gnuradio_python`) —
Aithernet's venv never imports `gnuradio`.

* **Duplicate-discovery dedup** — a board a tool lists twice (SoapyPlutoSDR `PlutoSDR #0`/`#1` at
  one `uri`) collapses to **one** device record; identity uses stable facts (provider/driver/URI/
  serial), never the `#N` label or list index.
* **Qualification runs** — `aithernet hardware qualify run <device-id>` performs, on the real
  device: capability probe → deterministic backend binding → full lease lifecycle → TX-exclusive
  **lease** check (no RF emitted) → a bounded **real RX capture** imported into the artifact store
  with full provenance (device, capability revision, lease, backend, RF params, SHA-256).
* **Reference missions** — RX capture, a 2.4 GHz energy survey (per-channel median/percentile
  power + occupancy; an energy scan measures aggregate activity, not emitter identity), and
  receive-chain restart recovery.
* **Honest support matrix** — `discovered → probed → rx_qualified → tx_lease_qualified →
  capture_qualified → survey_qualified → disconnect_recovery_qualified` (+ `unsupported`/`failed`/
  `not_executed`); never "supported" just because a device was discovered.
* **Surfaces** — read-only `GET /hardware/qualifications[/{id}[/checks]]`, `/hardware/support-matrix`,
  `/hardware/measurements`, `/hardware/devices/{id}/qualification-status`, an `aithernet hardware
  qualify` CLI, and a dashboard **Qualification & support** card. No raw probe output, device
  paths, credentials, or absolute paths are exposed; the external RF backends are never modified.

## Multi-node field operation & soak validation (Stage 14C.2)

Stage 14C.2 validates the complete node under realistic multi-node, repeated-operation, failure,
and long-duration conditions, with an honest acceptance classification (`software_harness_complete
→ single_device_soak_complete → multi_node_ip_complete → two_device_rf_complete →
extended_soak_complete`, plus `blocked`/`failed`). No fake result is ever counted as physical
evidence; with a single SDR, two-radio RF exchange is classified `not_executed`.

* **Field campaigns** — `aithernet field campaign create/start/status/stop/report`. A persistent
  `FieldCampaign` records a bounded sanitized topology (node identities, sanitized host ids, device
  ids, backend revisions), thresholds, and classification. A single-SDR repeated campaign cycles
  inventory refresh → RX lease → bounded **real capture** → artifact import → lease release → rest,
  recording per-iteration provenance (device, capability revision, lease, freq/rate/gain, expected
  vs actual samples, bytes, SHA-256, latency, cleanup, RSS/disk). Sampling and lease cycling create
  **no MissionStep**.
* **Resource soak** — `aithernet field soak start`: bounded `/proc`-based sampling (RSS, FDs,
  threads, children, DB/WAL/artifact/log sizes, events, outbox, leases) with explicit configured
  thresholds (Part O) detecting leaked leases, orphan subprocesses, FD/RSS/DB growth, and a
  disk-floor violation. No cloud upload.
* **Fault injection** — `aithernet field fault inject`: OFF by default, validation-mode only,
  bounded predefined faults, confirmation required — never an arbitrary shell/network command.
* **Multi-node IP** — independent OS-process nodes coordinate over the existing signed transport
  (mission delegation, remote status, semantic reply, durable artifact transfer, restart).
* **Surfaces** — read-only `GET /field/campaigns[/{id}[/checks|measurements|faults|nodes|report]]`,
  an `aithernet field` CLI, and a dashboard **Field** view (campaigns, classification, per-check
  pass/fail/not-executed as accessible text). No credentials, lease tokens, private identity, raw
  paths, or command output are exposed; the external RF backends are never modified.

## Roadmap

Future stages will add **authentication/access control**, **autonomous multi-step loops**
(chaining mission steps on their own), **long-lived MCP sessions and GNU Radio domain
tooling**, and **outbound callbacks and a peer mesh** (the node calling agents back /
multi-node coordination — building on the Stage 7 connection layer and stored
`endpoint_url`) — building on the runtime, state store, event bus, coordinator, coding
agent, GNU Radio MCP client, decision-routing, integration diagnostics, external-agent
connection interface, and web dashboard established in Stages 1–8.
