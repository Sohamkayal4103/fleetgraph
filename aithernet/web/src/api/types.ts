// TypeScript mirrors of the Aithernet backend response/request schemas. These describe the
// REAL API surface (FastAPI + Pydantic); the dashboard never invents data not returned by
// the backend.

export interface HealthStatus {
  status: string
  service: string
  version: string
}

export interface NodeStatus {
  node_id: string
  node_name: string
  runtime_status: string
  database_path: string
  started_at: string
  version: string
  mission_count: number
  event_count: number
}

export interface CoordinatorStatus {
  provider: string
  model: string | null
  configured: boolean
  missing_configuration: string[]
  executable: string | null
  cli_version: string | null
  base_url_configured: boolean
  api_key_configured: boolean
  active_capabilities: string[]
  future_capabilities: string[]
}

export interface CoordinatorProbeResult {
  attempted: boolean
  succeeded: boolean | null
  response_received: boolean | null
  model_reported: string[] | null
  tool_calls: number | null
  elapsed_ms: number | null
  exit_code: number | null
  error_type: string | null
  error: string | null
}

export interface CoordinatorDiagnostics {
  provider: string
  configured: boolean
  missing_configuration: string[]
  model: string
  timeout_seconds: number
  executable: string | null
  executable_resolves: boolean | null
  resolved_executable_path: string | null
  cli_version: string | null
  working_directory: string | null
  base_url_configured: boolean
  api_key_configured: boolean
  probe: CoordinatorProbeResult
  hints: string[]
}

export interface CodingAgentStatus {
  provider: string
  configured: boolean
  executable: string
  workspace: string
  missing_configuration: string[]
  active_capabilities: string[]
  future_capabilities: string[]
}

export type CodingTaskStatus = 'created' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface CodingTask {
  id: string
  mission_id: string | null
  objective: string
  context: Record<string, unknown>
  available_tools: string[]
  expected_outputs: string[]
  reporting_requirements: string[]
  status: CodingTaskStatus
  provider: string | null
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
}

export interface CodingTaskResult {
  id: string
  task_id: string
  status: CodingTaskStatus
  summary: string
  stdout: string
  stderr: string
  exit_code: number | null
  artifacts: unknown[]
  files_changed: unknown[]
  commands_run: unknown[]
  payload: Record<string, unknown>
  created_at: string
}

export interface CodingTaskRunResponse {
  task: CodingTask
  result: CodingTaskResult
}

export type MissionStatus =
  | 'received'
  | 'queued'
  | 'active'
  | 'waiting'
  | 'paused'
  | 'completed'
  | 'blocked'
  | 'failed'
  | 'cancelled'

export interface Mission {
  id: string
  source_type: string
  source_id: string | null
  content: string
  status: MissionStatus
  created_at: string
  updated_at: string
  metadata: Record<string, unknown>
}

export interface MissionCreate {
  content: string
  source_type?: string
  source_id?: string | null
  metadata?: Record<string, unknown>
}

export interface CoordinatorDecision {
  decision_id: string
  mission_id: string
  summary: string
  next_target: string
  action: string
  message: string
  structured_payload: Record<string, unknown>
  expected_result: string
  confidence: number | null
  created_at: string
}

export type MissionStepStatus = 'created' | 'running' | 'completed' | 'failed' | 'blocked'

export interface MissionStep {
  id: string
  mission_id: string
  decision_id: string
  decision: Record<string, unknown>
  route_target: string
  route_action: string
  status: MissionStepStatus
  result: Record<string, unknown>
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface MissionStepRunResponse {
  mission: Mission
  decision: CoordinatorDecision
  step: MissionStep
}

// -- autonomous execution (Stage 12) ---------------------------------------------

export type MissionRunStatus =
  | 'queued'
  | 'active'
  | 'waiting'
  | 'paused'
  | 'completed'
  | 'blocked'
  | 'failed'
  | 'cancelled'

// The execution run as returned by the API. Lease TOKENS are never serialized — only the
// owner id and lease timestamps appear here.
export interface MissionExecutionRun {
  id: string
  mission_id: string
  node_id: string
  status: MissionRunStatus
  iteration_count: number
  coordinator_call_count: number
  node_state_action_count: number
  coding_agent_action_count: number
  mcp_action_count: number
  response_action_count: number
  consecutive_failures: number
  elapsed_seconds: number
  budgets: Record<string, number>
  last_mission_step_id: string | null
  last_decision_id: string | null
  last_route_target: string | null
  waiting: Record<string, unknown> | null
  blocked_reason: string | null
  failure_type: string | null
  failure_message: string | null
  final_response: string | null
  pause_requested: boolean
  recovery_count: number
  version: number
  execution_owner_id: string | null
  lease_acquired_at: string | null
  lease_renewed_at: string | null
  lease_expiry: string | null
  created_at: string
  queued_at: string | null
  started_at: string | null
  last_activity_at: string | null
  waiting_at: string | null
  paused_at: string | null
  completed_at: string | null
  failed_at: string | null
  cancelled_at: string | null
  cancel_requested_at: string | null
  cancel_acknowledged_at: string | null
}

export interface MissionExecutionStatus {
  mission: Mission
  run: MissionExecutionRun | null
  budget_remaining: Record<string, number>
  has_active_run: boolean
}

export interface MissionStartResponse {
  mission: Mission
  run: MissionExecutionRun
  queued: boolean
  detail: string
}

export interface MissionTimelineEntry {
  at: string
  kind: string
  event_type: string | null
  source: string | null
  message: string
  step_id: string | null
  route_target: string | null
  status: string | null
}

export interface MissionTimeline {
  mission_id: string
  entries: MissionTimelineEntry[]
}

export interface MissionWorkerStatus {
  enabled: boolean
  running: boolean
  degraded: boolean
  node_id: string
  worker_count: number
  active_missions: string[]
  active_count: number
  queued_count: number
  last_error: string | null
}

export interface NodeEvent {
  id: string
  mission_id: string | null
  event_type: string
  source: string
  message: string
  payload: Record<string, unknown>
  created_at: string
}

// -- MCP ----------------------------------------------------------------------

export interface McpServerStatus {
  provider: string
  configured: boolean
  command: string | null
  server_name: string | null
  missing_configuration: string[]
  active_capabilities: string[]
  available_tools: string[] | null
  future_capabilities: string[]
}

export interface McpProbeResult {
  attempted: boolean
  succeeded: boolean | null
  tool_count: number | null
  tool_names: string[] | null
  error_type: string | null
  error: string | null
}

export interface McpDiagnostics {
  provider: string
  configured: boolean
  command: string | null
  command_resolves: boolean
  resolved_command_path: string | null
  args_unresolved_count: number
  missing_configuration: string[]
  cwd: string | null
  cwd_exists: boolean | null
  timeout_seconds: number
  probe: McpProbeResult
  hints: string[]
}

export interface McpToolInfo {
  name: string
  description: string | null
  input_schema: Record<string, unknown> | null
}

export type McpSessionState =
  | 'stopped'
  | 'starting'
  | 'ready'
  | 'degraded'
  | 'restarting'
  | 'stopping'
  | 'failed'

export interface McpSessionStatus {
  session_id: string
  node_id: string | null
  provider: string
  state: McpSessionState
  configured: boolean
  command: string | null
  pid: number | null
  generation: number
  restart_count: number
  started_at: string | null
  ready_at: string | null
  stopped_at: string | null
  last_activity_at: string | null
  uptime_seconds: number | null
  tool_count: number | null
  tool_catalog_generation: number | null
  active_requests: number
  max_in_flight_requests: number
  last_error_type: string | null
  last_error: string | null
  autostart: boolean
  auto_restart: boolean
  missing_configuration: string[]
}

export interface GnuRadioContext {
  context_id: string
  node_id: string
  active_flowgraph_path: string | null
  active_flowgraph_name: string | null
  active_flowgraph_hash: string | null
  flowgraph_status: string
  block_summary: Record<string, unknown> | null
  connection_summary: Record<string, unknown> | null
  validation_summary: Record<string, unknown> | null
  latest_error_summary: Record<string, unknown> | null
  execution_summary: Record<string, unknown> | null
  related_mission_id: string | null
  related_mission_step_id: string | null
  source_mcp_call_ids: string[]
  source_session_id: string | null
  source_session_generation: number | null
  stale: boolean
  stale_reason: string | null
  context_version: number
  last_refreshed_at: string | null
  last_changed_at: string | null
}

export type McpToolCallStatus = 'created' | 'running' | 'completed' | 'failed'

export interface McpToolCall {
  id: string
  mission_id: string | null
  task_id: string | null
  caller: string
  tool_name: string
  arguments: Record<string, unknown>
  status: McpToolCallStatus
  result: Record<string, unknown>
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface McpToolCallBody {
  mission_id?: string | null
  task_id?: string | null
  caller?: string
  arguments?: Record<string, unknown>
}

// -- External agents ----------------------------------------------------------

export type ExternalAgentStatusValue = 'connected' | 'disconnected' | 'disabled'

export interface ExternalAgent {
  id: string
  name: string
  agent_type: string
  endpoint_url: string | null
  transport: string
  auth_type: string | null
  status: ExternalAgentStatusValue
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
  last_seen_at: string | null
}

export interface ExternalAgentCreate {
  name: string
  agent_type?: string
  endpoint_url?: string | null
  transport?: string
  auth_type?: string
  metadata?: Record<string, unknown>
}

export interface ExternalAgentMessage {
  id: string
  agent_id: string
  direction: 'inbound' | 'outbound'
  message_type: string
  content: string
  payload: Record<string, unknown>
  mission_id: string | null
  created_at: string
}

export interface ExternalAgentMessageCreate {
  message_type?: string
  content: string
  payload?: Record<string, unknown>
  create_mission?: boolean
  run_step?: boolean
}

export interface ExternalAgentMessageResponse {
  agent: ExternalAgent
  message: ExternalAgentMessage
  mission: Mission | null
  step_response: MissionStepRunResponse | null
}

export interface ExternalAgentStatusSnapshot {
  connected_count: number
  total_count: number
  agents: ExternalAgent[]
}

// -- Stage 13A agent transport ---------------------------------------------------

export interface IdentityStatus {
  initialized: boolean
  node_id?: string | null
  node_name?: string | null
  fingerprint?: string | null
  fingerprint_short?: string | null
  public_key?: string | null
  created_at?: string | null
  state_directory?: string | null
}

export interface Peer {
  id: string
  name: string
  role: string
  endpoint_url: string | null
  expected_node_id: string | null
  public_key: string | null
  fingerprint: string | null
  trust_state: string
  enabled: boolean
  transport: string
  tls_verify: boolean
  status: string
  consecutive_failures: number
  last_success_at: string | null
  last_failure_at: string | null
  last_error: string | null
  capability_snapshot: Record<string, unknown> | null
  capability_snapshot_at: string | null
  last_manifest_at: string | null
  created_at: string
  updated_at: string
}

export interface PeerCreate {
  name: string
  role?: string
  endpoint_url?: string | null
  expected_node_id?: string | null
  public_key?: string | null
  fingerprint?: string | null
  tls_verify?: boolean
}

export interface OutboxMessage {
  id: string
  message_id: string
  peer_id: string
  kind: string
  status: string
  attempt_count: number
  max_attempts: number
  conversation_id: string | null
  mission_id: string | null
  next_attempt_at: string | null
  delivered_at: string | null
  acknowledged_at: string | null
  error_type: string | null
  last_error: string | null
  dead_letter_reason: string | null
  created_at: string
}

export interface InboxMessage {
  id: string
  message_id: string
  peer_id: string | null
  sender_node_id: string
  sender_fingerprint: string
  kind: string
  duplicate_count: number
  rejection_reason: string | null
  received_at: string
}

export interface TransportWorkerStatus {
  enabled: boolean
  running: boolean
  degraded: boolean
  worker_count: number
  in_flight: number
  last_error: string | null
}

export interface TransportStatus {
  enabled: boolean
  identity: IdentityStatus
  worker: TransportWorkerStatus
  outbox_counts: Record<string, number>
  inbox_count: number
  peer_count: number
  trusted_peer_count: number
}

export interface AgentMessageSendRequest {
  peer_id: string
  subject?: string | null
  text?: string | null
  data?: Record<string, unknown>
  send_now?: boolean
}

export interface AgentMessageSendResponse {
  message: OutboxMessage
  queued: boolean
  detail: string
}

// -- Stage 13A.5 RF backends -----------------------------------------------------

export interface RFBackendStatus {
  backend_id: string
  display_name: string
  backend_kind: string
  experimental: boolean
  enabled: boolean
  default: boolean
  state: string
  provider: string
  command: string | null
  cwd: string | null
  version: string | null
  source_revision: string | null
  session_id: string | null
  session_generation: number | null
  pid: number | null
  tool_count: number
  active_requests: number
  max_in_flight_requests: number
  workspace: string | null
  last_activity_at: string | null
  last_error_type: string | null
  last_error: string | null
}

export interface RFToolInfo {
  name: string
  description: string | null
  input_schema: Record<string, unknown> | null
}

export interface RFContext {
  backend_id: string
  status?: string
  stale: boolean
  stale_reason?: string | null
  artifact_count?: number
  [key: string]: unknown
}

export interface RFArtifact {
  id: string
  backend_id: string
  relative_path: string
  artifact_kind: string
  media_type: string | null
  size_bytes: number | null
  created_at: string
}

// -- Stage 13B coordinator communications ----------------------------------------

export interface ConversationSummary {
  conversation_id: string
  outbound: number
  inbound: number
  peers: string[]
}

export interface InboundRequest {
  request_id: string
  peer_id: string
  sender_node_id: string
  conversation_id: string | null
  mission_id: string | null
  response_required: boolean
  response_queued: boolean
  state: string
  rejection_reason: string | null
  created_at: string
}

export interface ReplyWait {
  wait_id: string
  mission_id: string
  request_id: string
  outbound_message_id: string
  expected_peer_id: string
  conversation_id: string | null
  state: string
  deadline: string | null
  satisfying_inbox_message_id: string | null
  created_at: string
  reason: string | null
}

export interface CommMessageSummary {
  direction: string
  message_id: string
  peer_id: string | null
  message_type: string | null
  conversation_id: string | null
  request_id: string | null
  reply_to_request_id?: string | null
  expects_reply?: boolean
  delivery_status?: string
  transport_acknowledged?: boolean
  semantic_reply?: boolean
  application_state?: string
  duplicate_count?: number
  at: string | null
}

export interface MissionCommunications {
  outbound_messages: CommMessageSummary[]
  reply_waits: ReplyWait[]
  recent_conversation: CommMessageSummary[]
  response_obligation: { request_id: string; response_required: boolean; response_queued: boolean } | null
}

export interface PeerPermissions {
  peer_id: string
  name: string
  trust_state: string
  enabled: boolean
  may_send_requests: boolean
  may_send_replies: boolean
  may_receive_messages: boolean
  may_request_response: boolean
  max_inbound_request_bytes: number | null
  max_concurrent_inbound_missions: number | null
}

// -- Stage 13C operator dashboard aggregate read models ---------------------------
// All bounded + sanitized: fingerprints are abbreviated, message content is a bounded
// preview, and no field ever carries a key, signature, raw envelope, prompt, or env.

export interface OverviewWorker {
  enabled: boolean | null
  running: boolean | null
  degraded: boolean | null
  active_count?: number | null
  queued_count?: number | null
  in_flight?: number | null
}

export interface Overview {
  node: {
    node_id: string | null
    node_name: string | null
    runtime_status: string | null
    version: string | null
    started_at: string | null
    mission_count: number | null
    event_count: number | null
    identity_initialized: boolean
    fingerprint: string | null
  }
  workers: { mission: OverviewWorker; transport: OverviewWorker }
  coordinator: { provider: string | null; configured: boolean | null; missing_configuration: string[] }
  coding_agent: { provider: string | null; configured: boolean | null; missing_configuration: string[] }
  rf_backends: Array<{
    backend_id: string
    state: string
    tool_count: number
    experimental: boolean
    version: string | null
    last_error_type: string | null
  }>
  peers: { total?: number; trusted?: number }
  outbox: Record<string, number>
  inbox_count: number
  missions: { by_status?: Record<string, number>; active?: number; waiting?: number; queued?: number }
  reply_waits: { pending?: number; by_state?: Record<string, number> }
  inbound_requests: { total?: number; by_state?: Record<string, number> }
  recent_failures: {
    communication?: FailureEntry[]
    rf?: FailureEntry[]
    mission?: FailureEntry[]
  }
}

export interface FailureEntry {
  event_type: string
  source: string
  message: string | null
  mission_id: string | null
  at: string | null
}

export interface FleetPeerPermissions {
  may_send_requests: boolean
  may_send_replies: boolean
  may_receive_messages: boolean
  may_request_response: boolean
  max_inbound_request_bytes: number | null
  max_concurrent_inbound_missions: number | null
}

export interface FleetPeer {
  peer_id: string
  name: string
  role: string
  trust_state: string
  enabled: boolean
  transport_health: string
  last_success_at: string | null
  last_failure_at: string | null
  consecutive_failures: number
  fingerprint: string | null
  manifest_at: string | null
  capabilities: unknown
  permissions: FleetPeerPermissions
  conversation_count: number
  outstanding_messages: number
  pending_reply_waits: number
}

export interface Fleet {
  peers: FleetPeer[]
}

export interface TimelineEntry {
  kind: string
  direction?: string
  message_id?: string
  message_type?: string | null
  peer_id?: string | null
  text_preview?: string | null
  data_preview?: string | null
  request_id?: string | null
  reply_to_request_id?: string | null
  expects_reply?: boolean | null
  delivery_status?: string
  transport_acknowledged?: boolean
  semantic_reply?: boolean
  application_state?: string
  duplicate_count?: number
  attempt_count?: number
  error_type?: string | null
  mission_id?: string | null
  mission_run_id?: string | null
  mission_step_id?: string | null
  // reply-wait entries
  wait_id?: string
  outbound_message_id?: string
  expected_peer_id?: string
  state?: string
  satisfying_inbox_message_id?: string | null
  deadline?: string | null
  // event entries
  event_type?: string
  source?: string
  message?: string | null
  route_target?: string
  route_action?: string
  status?: string
  step_id?: string
  at: string | null
}

export interface ConversationTimeline {
  conversation_id: string
  peers: string[]
  message_count: number
  entries: TimelineEntry[]
  legend: Record<string, string>
}

export interface TopologyNode {
  id: string
  kind: string
  label: string
  status?: string | null
}

export interface TopologyEdge {
  from: string
  to: string
  kind: string
  request_id?: string | null
  acknowledged?: boolean
  satisfied?: boolean
  state?: string
  response_required?: boolean
  response_queued?: boolean
}

export interface DistributedMissionTimeline {
  mission_id: string
  mission_status: string
  mission_run_id: string | null
  iteration_count: number | null
  source_type: string
  is_inbound_mission: boolean
  outbound_messages: CommMessageSummary[]
  reply_waits: ReplyWait[]
  response_obligation: { request_id: string; response_required: boolean; response_queued: boolean } | null
  inbound_request: InboundRequest | null
  entries: TimelineEntry[]
  topology: { nodes: TopologyNode[]; edges: TopologyEdge[] }
  legend: Record<string, string>
}

export interface CommunicationsSummary {
  outbox_counts: Record<string, number>
  inbox_count: number
  reply_waits: Record<string, number>
  inbound_requests: { total?: number; by_state?: Record<string, number> }
  peers: { total?: number; trusted?: number }
  recent_failures: FailureEntry[]
}

export interface OperatorMessageRequest {
  peer_id: string
  message_type: 'request' | 'update' | 'reply'
  text: string
  data?: Record<string, unknown> | null
  expects_reply?: boolean
  response_deadline?: string | null
  conversation_id?: string | null
  reply_to_request_id?: string | null
}

export interface OperatorMessageResponse {
  message_id: string
  request_id: string
  conversation_id: string | null
  peer_id: string
  status: string
  expects_reply: boolean
  origin: string
}

// -- Stage 13D.2 cross-node artifact transfer -------------------------------------
// Metadata only — no bytes, no absolute paths, no keys/signatures/grant material.

export interface ArtifactSummary {
  artifact_id: string
  node_id: string
  origin_node_id: string | null
  origin_artifact_id: string | null
  backend_id: string
  artifact_kind: string
  media_type: string | null
  display_name: string | null
  size_bytes: number | null
  digest: string | null
  availability_state: string
  pinned: boolean
  retention_state: string
  conversation_id: string | null
  mission_id: string | null
  created_at: string | null
}

export interface RemoteArtifactOffer {
  id: string
  peer_id: string
  origin_node_id: string | null
  origin_artifact_id: string
  digest: string
  size_bytes: number
  artifact_kind: string | null
  display_name: string | null
  state: string
  conversation_id: string | null
  offered_at: string | null
}

export interface ArtifactTransfer {
  transfer_id: string
  direction: string
  peer_id: string
  state: string
  local_artifact_id: string | null
  origin_artifact_id: string | null
  expected_digest: string
  expected_size: number
  received_bytes: number
  artifact_kind: string | null
  display_name: string | null
  conversation_id: string | null
  mission_id: string | null
  attempt_count: number
  error_type: string | null
  last_error: string | null
  grant_expires_at: string | null
  created_at: string | null
  completed_at: string | null
}

export interface ArtifactStoreStatus {
  total_quota_bytes: number
  used_bytes: number
  available_in_quota_bytes: number
  free_disk_bytes: number
  minimum_free_bytes: number
  max_artifact_bytes: number
}

export interface ArtifactPermissions {
  peer_id: string
  name: string
  trust_state: string
  may_offer_artifacts: boolean
  may_request_artifacts: boolean
  may_receive_artifacts: boolean
  max_artifact_bytes: number | null
  max_concurrent_transfers: number | null
  allowed_artifact_kinds: string[] | null
}

// -- Stage 13D.3 remote mission-status synchronization ---------------------------
// Bounded + sanitized: ids, states, sequence numbers, counts only — never prompts, tool
// contents, raw envelopes, signatures, keys, paths, credentials, or environments.

export type Freshness = 'unknown' | 'fresh' | 'stale' | 'terminal'

export interface RemoteMissionSnapshot {
  snapshot_id: string
  origin_node_id: string
  peer_id: string
  remote_mission_ref: string
  request_id: string | null
  parent_request_id: string | null
  conversation_id: string | null
  local_mission_id: string | null
  latest_sequence: number
  state: string | null
  response_obligation: string | null
  progress_summary: string | null
  terminal_category: string | null
  freshness: Freshness
  artifact_count: number
  transfer_count: number
  artifact_ids: string[]
  transfer_ids: string[]
  remote_updated_at: string | null
  received_at: string | null
  freshness_deadline: string | null
  query_pending: boolean
  protocol_version: string
}

export interface RemoteMissionStatusEvent {
  event_id: string
  sequence: number | null
  state: string | null
  message_type: string | null
  disposition: string
  reason: string | null
  peer_id: string
  received_at: string | null
}

export interface MissionStatusPublication {
  publication_id: string
  mission_id: string
  peer_id: string
  request_id: string | null
  last_sequence: number
  last_state: string | null
  updated_at: string | null
}

export interface MissionStatusPermissions {
  peer_id: string
  name: string
  trust_state: string
  enabled: boolean
  may_publish_mission_status: boolean
  may_query_mission_status: boolean
  may_receive_mission_status: boolean
  max_active_remote_snapshots: number | null
}

// -- Stage 14A operations -------------------------------------------------------

export interface OpsComponent {
  name: string
  required: boolean
  state: string
  detail: string | null
}

export interface OpsOverview {
  version: string
  node_id: string
  node_name: string
  started_at: string
  generated_at: string
  readiness: { phase: string; ready: boolean; components: OpsComponent[] }
  migrations: { applied?: number; pending?: string[]; schema_valid?: boolean; error?: string }
  storage: {
    path?: string
    total_bytes?: number
    used_bytes?: number
    free_bytes?: number
    free_pct?: number | null
    error?: string
  }
  backups: { count: number; latest_created_at: string | null; latest_version: string | null }
  state_root_configured: boolean
}

export interface OpsPreflightCheck {
  name: string
  category: string
  status: string
  detail: string | null
}

export interface OpsPreflight {
  ok: boolean
  counts: Record<string, number>
  checks: OpsPreflightCheck[]
}

export interface OpsBackup {
  path: string
  bytes: number
  node_id?: string
  created_at?: string
  app_version?: string
  includes_private_identity?: boolean
  file_count?: number
}

// -- Managed SDR hardware (Stage 14B) ---------------------------------------------

export interface HardwareProvider {
  provider_id: string
  kind: string
  enabled: boolean
  required: boolean
  available: boolean
}

export interface HardwareDevice {
  device_id: string
  node_id: string
  hardware_key: string
  provider_id: string
  device_kind: string
  vendor: string | null
  product: string | null
  serial: string | null
  driver: string | null
  transport: string | null
  channel: string | null
  display_name: string | null
  identity_limited: boolean
  enabled: boolean
  administrative_state: string
  presence_state: string
  health_state: string
  status: string
  capability_revision: number
  rx: boolean | null
  tx: boolean | null
  channel_count: number | null
  metadata: Record<string, unknown>
  compatible_backends: string[]
  active_lease_count: number
  first_seen_at: string
  last_seen_at: string | null
}

export interface HardwareCapability {
  device_id: string
  revision: number
  rx_supported: boolean | null
  tx_supported: boolean | null
  full_duplex: boolean | null
  channel_count: number | null
  shared_receive_safe: boolean
  source: string
  confidence: string
  capabilities: Record<string, unknown>
  probed_at: string | null
}

export interface HardwareHealthEvent {
  id: string
  device_id: string
  health_state: string
  previous_health_state: string | null
  presence_state: string | null
  detail: string | null
  created_at: string
}

export interface HardwareLease {
  lease_id: string
  node_id: string
  device_id: string
  backend_id: string | null
  mission_id: string | null
  mission_run_id: string | null
  mission_step_id: string | null
  requested_operation: string | null
  direction: string
  requested_channels: number[]
  lease_mode: string
  state: string
  reason: string | null
  acquired_at: string | null
  renewed_at: string | null
  expires_at: string | null
  released_at: string | null
  created_at: string
}

export interface HardwareStatus {
  enabled: boolean
  operator_lease_actions_enabled: boolean
  providers: HardwareProvider[]
  devices_by_status: Record<string, number>
  leases_by_state: Record<string, number>
}

export interface HardwareRefreshResult {
  availability: Record<string, boolean>
  devices_by_status: Record<string, number>
}

export interface HardwareLeaseAcquire {
  device_id: string
  backend_id: string
  direction?: string
  lease_mode?: string
  operation?: string | null
}

// -- Hardware qualification (Stage 14C.1) -----------------------------------------

export interface HardwareSupportEntry {
  device_id: string
  display_name: string | null
  provider_id: string
  driver: string | null
  architecture_supported: boolean
  discovered: boolean
  classification: string
  last_qualified_at: string | null
}

export interface HardwareQualificationRun {
  id: string
  node_id: string
  device_id: string
  hardware_key: string | null
  provider_id: string | null
  status: string
  support_classification: string
  capability_revision: number | null
  environment: Record<string, unknown>
  summary: string | null
  warnings: unknown[]
  checks_total: number
  checks_passed: number
  checks_failed: number
  checks_not_executed: number
  started_at: string
  completed_at: string | null
}

export interface HardwareQualificationCheck {
  id: string
  run_id: string
  name: string
  category: string
  status: string
  detail: string | null
  evidence: Record<string, unknown>
  created_at: string
}

export interface HardwareMeasurement {
  id: string
  device_id: string
  qualification_run_id: string | null
  kind: string
  center_frequency_hz: number | null
  sample_rate_sps: number | null
  gain_db: number | null
  antenna: string | null
  capture_bytes: number | null
  sample_count: number | null
  sha256: string | null
  artifact_id: string | null
  confidence: string
  created_at: string
}

// -- Field validation (Stage 14C.2) -----------------------------------------------

export interface FieldCampaign {
  id: string
  node_id: string
  name: string
  objective: string | null
  profile: string
  test_mode: string
  status: string
  classification: string
  software_version: string | null
  config_digest: string | null
  topology: Record<string, unknown>
  thresholds: Record<string, unknown>
  warnings: unknown[]
  summary: string | null
  checks_passed: number
  checks_failed: number
  checks_not_executed: number
  started_at: string | null
  completed_at: string | null
  created_at: string
}

export interface FieldCheck {
  name: string
  category: string
  status: string
  classification: string | null
  detail: string | null
  evidence: Record<string, unknown>
  created_at: string
}

// Stage 14D: external-agent interoperability (read-only; never secrets/tokens/paths).
export interface InteropAgent {
  agent_id: string
  display_name: string
  status: string
  identity_type: string
  fingerprint: string | null
  owner: string | null
  permissions: string[]
  endpoint_policy: string
  last_authenticated_at: string | null
  last_delivery_at: string | null
  created_at: string | null
}

export interface InteropEndpoint {
  endpoint_id: string
  host: string
  scheme: string
  port: number | null
  status: string
  policy_result: Record<string, unknown> | null
  verified_at: string | null
}

export interface InteropSubscription {
  subscription_id: string
  delivery_mode: string
  status: string
  filters: Record<string, unknown>
  endpoint_id: string | null
  next_sequence: number
  last_acked_sequence: number
  created_at: string | null
}

export interface InteropDelivery {
  message_pk: string
  message_id: string
  event_type: string
  delivery_mode: string
  subscription_id: string
  sequence: number
  status: string
  attempt_count: number
  max_attempts: number
  last_status_code: number | null
  failure_category: string | null
  mission_id: string | null
  artifact_id: string | null
  next_attempt_at: string | null
  delivered_at: string | null
  acknowledged_at: string | null
  created_at: string | null
}

export interface InteropDiagnostics {
  enabled: boolean
  callbacks_enabled: boolean
  websocket_enabled: boolean
  worker_running: boolean
  worker_degraded: boolean
  live_websocket_connections: number
  agents_total: number
  agents_active: number
  messages_by_status: Record<string, number>
  pending_messages: number
  dead_letter_messages: number
  open_websocket_sessions: number
}

// Stage 14E: data & privacy (read-only; never secrets/tokens/keys/raw paths).
export interface DataCategoryConsent {
  category: string
  collection_allowed: boolean
  export_allowed: boolean
  raw_artifact_allowed: boolean
  revocation_state: string
  reason: string
}

export interface DataConsentGrantView {
  grant_id: string
  category: string
  purpose: string
  collection_scope: string
  export_scope: string
  raw_artifact_scope: string
  revocation_state: string
}

export interface DataConsentView {
  effective: Record<string, DataCategoryConsent>
  grants: DataConsentGrantView[]
}

export interface DataRecord {
  record_id: string
  category: string
  status: string
  sensitivity: string
  byte_size: number
  retention_class: string
  redaction_policy_version: string
  created_at: string | null
}

export interface DataDestination {
  destination_id: string
  kind: string
  name: string
  enabled: boolean
  status: string
  readiness: string
  config: Record<string, unknown>
}

export interface DataBatch {
  batch_id: string
  destination_id: string
  status: string
  record_count: number
  compressed_bytes: number
  encrypted: boolean
  attempt_count: number
  max_attempts: number
  failure_category: string | null
}

export interface DataDeletion {
  deletion_id: string
  scope: string
  status: string
  local_deleted: number
  remote_state: string | null
}

export interface DataDataset {
  dataset_id: string
  name: string
  purpose: string
}

export interface DataDiagnostics {
  enabled: boolean
  tenant_id: string
  collection_paused: boolean
  export_enabled: boolean
  export_paused: boolean
  worker_running: boolean
  pending_records: number
  held_records: number
  pending_batches: number
  dead_letter_batches: number
  free_disk_bytes: number
  air_gapped: boolean
  drive_status: { configured: boolean; enabled: boolean; credentials_present: boolean }
}
