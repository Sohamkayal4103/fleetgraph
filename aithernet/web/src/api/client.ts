// A thin, typed client over the real Aithernet FastAPI backend. Every method maps to an
// actual endpoint; there is no mock layer and no demo mode. Errors are surfaced honestly
// and CATEGORIZED so the UI never mislabels an HTTP 500 as a network outage:
//   - 'network' : the backend could not be reached at all ("backend unavailable")
//   - 'timeout' : the request was aborted after the per-call timeout
//   - 'http'    : a real HTTP response with status >= 400, carrying the backend `detail`
//   - 'parse'   : a 2xx response whose body was not valid JSON

import type {
  AgentMessageSendRequest,
  AgentMessageSendResponse,
  CodingAgentStatus,
  CodingTask,
  CodingTaskRunResponse,
  CoordinatorDiagnostics,
  CoordinatorStatus,
  GnuRadioContext,
  ExternalAgent,
  ExternalAgentCreate,
  ExternalAgentMessage,
  ExternalAgentMessageCreate,
  ExternalAgentMessageResponse,
  ExternalAgentStatusSnapshot,
  ExternalAgentStatusValue,
  HealthStatus,
  IdentityStatus,
  InboxMessage,
  McpDiagnostics,
  McpServerStatus,
  McpSessionStatus,
  McpToolCall,
  McpToolCallBody,
  McpToolInfo,
  Mission,
  MissionCreate,
  MissionExecutionRun,
  MissionExecutionStatus,
  MissionStartResponse,
  MissionStep,
  MissionStepRunResponse,
  MissionTimeline,
  MissionWorkerStatus,
  NodeEvent,
  ArtifactSummary,
  ArtifactTransfer,
  ArtifactStoreStatus,
  ArtifactPermissions,
  RemoteArtifactOffer,
  RemoteMissionSnapshot,
  RemoteMissionStatusEvent,
  MissionStatusPublication,
  MissionStatusPermissions,
  ConversationSummary,
  ConversationTimeline,
  CommunicationsSummary,
  DistributedMissionTimeline,
  Fleet,
  InboundRequest,
  MissionCommunications,
  NodeStatus,
  OperatorMessageRequest,
  OperatorMessageResponse,
  OpsOverview,
  OpsPreflight,
  OpsBackup,
  OutboxMessage,
  Overview,
  InteropAgent,
  InteropDelivery,
  InteropEndpoint,
  InteropSubscription,
  InteropDiagnostics,
  DataConsentView,
  DataDiagnostics,
  DataRecord,
  DataDestination,
  DataBatch,
  DataDeletion,
  DataDataset,
  FieldCampaign,
  FieldCheck,
  HardwareCapability,
  HardwareDevice,
  HardwareHealthEvent,
  HardwareLease,
  HardwareLeaseAcquire,
  HardwareMeasurement,
  HardwareProvider,
  HardwareQualificationCheck,
  HardwareQualificationRun,
  HardwareRefreshResult,
  HardwareStatus,
  HardwareSupportEntry,
  Peer,
  PeerCreate,
  PeerPermissions,
  ReplyWait,
  RFArtifact,
  RFBackendStatus,
  RFContext,
  RFToolInfo,
  TransportStatus,
} from './types'

const DEFAULT_BASE_URL = 'http://127.0.0.1:8080'

export const API_BASE_URL = (
  import.meta.env.VITE_AITHERNET_API_BASE_URL ?? DEFAULT_BASE_URL
).replace(/\/+$/, '')

export type ApiErrorCategory = 'network' | 'timeout' | 'http' | 'parse'

/** Default request timeout. Slow operations override it (see the constants below). */
const DEFAULT_TIMEOUT_MS = 30_000
/** MCP probe / tool call / mission step: may launch an external server. */
const SLOW_TIMEOUT_MS = 120_000
/** A coding-agent run executes a full agent session. */
const CODING_TIMEOUT_MS = 900_000
/** Max characters of a backend error body to surface (kept readable/bounded). */
const ERROR_DETAIL_LIMIT = 1500

export class ApiError extends Error {
  /** HTTP status for category 'http' (and 'parse'); 0 for 'network'/'timeout'. */
  readonly status: number
  /** The backend's sanitized `detail` (or the closest available description). */
  readonly detail: string
  /** The request path that failed. */
  readonly path: string
  /** What kind of failure this is — drives how the UI labels it. */
  readonly category: ApiErrorCategory

  constructor(opts: {
    category: ApiErrorCategory
    detail: string
    path: string
    status?: number
  }) {
    super(ApiError.buildMessage(opts.category, opts.status ?? 0, opts.detail, opts.path))
    this.name = 'ApiError'
    this.category = opts.category
    this.status = opts.status ?? 0
    this.detail = opts.detail
    this.path = opts.path
  }

  /** True ONLY for a real network/unreachable failure (never for an HTTP 4xx/5xx). */
  get unreachable(): boolean {
    return this.category === 'network'
  }

  private static buildMessage(
    category: ApiErrorCategory,
    status: number,
    detail: string,
    path: string,
  ): string {
    switch (category) {
      case 'network':
        return `Backend unavailable at ${API_BASE_URL} (${detail}). Is the node running?`
      case 'timeout':
        return `Request to ${path} ${detail}.`
      case 'parse':
        return `Invalid response from ${path}: ${detail}`
      case 'http':
        return `[${status}] ${detail}`
    }
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
  } catch (err) {
    const e = err as Error
    if (e.name === 'AbortError' || controller.signal.aborted) {
      throw new ApiError({
        category: 'timeout',
        detail: `timed out after ${Math.round(timeoutMs / 1000)}s`,
        path,
      })
    }
    // A genuine connection failure — the only case that should say "backend unavailable".
    throw new ApiError({ category: 'network', detail: e.message || 'network error', path })
  } finally {
    clearTimeout(timer)
  }

  if (!response.ok) {
    throw new ApiError({
      category: 'http',
      status: response.status,
      detail: await extractDetail(response),
      path,
    })
  }
  if (response.status === 204) {
    return undefined as T
  }
  const text = await response.text()
  if (!text) {
    return undefined as T
  }
  try {
    return JSON.parse(text) as T
  } catch (err) {
    throw new ApiError({
      category: 'parse',
      status: response.status,
      detail: (err as Error).message,
      path,
    })
  }
}

/**
 * Extract a useful error detail. The body is read ONCE as text, so a secondary JSON parse
 * failure never hides a perfectly good text error. Order: JSON `detail` -> another explicit
 * error/message field -> raw text body -> HTTP status phrase.
 */
async function extractDetail(response: Response): Promise<string> {
  let raw = ''
  try {
    raw = await response.text()
  } catch {
    return response.statusText || `HTTP ${response.status}`
  }
  if (!raw.trim()) {
    return response.statusText || `HTTP ${response.status}`
  }
  try {
    const body = JSON.parse(raw)
    if (body && typeof body === 'object') {
      const detail = (body as { detail?: unknown }).detail
      if (typeof detail === 'string') return detail.slice(0, ERROR_DETAIL_LIMIT)
      if (detail !== undefined) return JSON.stringify(detail).slice(0, ERROR_DETAIL_LIMIT)
      const alt = (body as { error?: unknown; message?: unknown }).error
        ?? (body as { message?: unknown }).message
      if (typeof alt === 'string') return alt.slice(0, ERROR_DETAIL_LIMIT)
      if (alt !== undefined) return JSON.stringify(alt).slice(0, ERROR_DETAIL_LIMIT)
    }
  } catch {
    // Not JSON — fall through to the raw text body.
  }
  return raw.slice(0, ERROR_DETAIL_LIMIT)
}

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') {
      search.set(key, String(value))
    }
  }
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

export const api = {
  baseUrl: API_BASE_URL,
  streamUrl: `${API_BASE_URL}/events/stream`,

  // Node
  getHealth: () => request<HealthStatus>('GET', '/health'),
  getNodeStatus: () => request<NodeStatus>('GET', '/node/status'),

  // Coordinator / coding agent
  getCoordinatorStatus: () => request<CoordinatorStatus>('GET', '/coordinator/status'),
  getCoordinatorDiagnostics: (probe = false) =>
    request<CoordinatorDiagnostics>(
      'GET',
      `/coordinator/diagnostics${query({ probe })}`,
      undefined,
      probe ? SLOW_TIMEOUT_MS : DEFAULT_TIMEOUT_MS,
    ),
  getCodingAgentStatus: () => request<CodingAgentStatus>('GET', '/coding-agent/status'),
  getCodingTasks: () => request<CodingTask[]>('GET', '/coding-tasks'),
  getCodingTask: (id: string) => request<CodingTask>('GET', `/coding-tasks/${id}`),
  runCodingTask: (body: { objective: string; mission_id?: string | null }) =>
    request<CodingTaskRunResponse>('POST', '/coding-tasks/run', body, CODING_TIMEOUT_MS),

  // MCP
  getMcpStatus: () => request<McpServerStatus>('GET', '/mcp/status'),
  getMcpDiagnostics: (probe = false) =>
    request<McpDiagnostics>(
      'GET',
      `/mcp/diagnostics${query({ probe })}`,
      undefined,
      probe ? SLOW_TIMEOUT_MS : DEFAULT_TIMEOUT_MS,
    ),
  getMcpTools: (refresh = false) =>
    request<McpToolInfo[]>(
      'GET',
      `/mcp/tools${query({ refresh })}`,
      undefined,
      SLOW_TIMEOUT_MS,
    ),
  callMcpTool: (toolName: string, body: McpToolCallBody) =>
    request<McpToolCall>(
      'POST',
      `/mcp/tools/${encodeURIComponent(toolName)}/call`,
      body,
      SLOW_TIMEOUT_MS,
    ),
  getMcpToolCalls: () => request<McpToolCall[]>('GET', '/mcp/tool-calls'),

  // MCP persistent session (Stage 11B)
  getMcpSession: () => request<McpSessionStatus>('GET', '/mcp/session'),
  startMcpSession: () =>
    request<McpSessionStatus>('POST', '/mcp/session/start', undefined, SLOW_TIMEOUT_MS),
  restartMcpSession: () =>
    request<McpSessionStatus>('POST', '/mcp/session/restart', undefined, SLOW_TIMEOUT_MS),
  stopMcpSession: () =>
    request<McpSessionStatus>('POST', '/mcp/session/stop', undefined, SLOW_TIMEOUT_MS),

  // GNU Radio workspace context (Stage 11B)
  getGnuRadioContext: () => request<GnuRadioContext>('GET', '/gnuradio/context'),
  refreshGnuRadioContext: () =>
    request<GnuRadioContext>('POST', '/gnuradio/context/refresh', {}, SLOW_TIMEOUT_MS),

  // Missions
  createMission: (body: MissionCreate) => request<Mission>('POST', '/missions', body),
  getMissions: () => request<Mission[]>('GET', '/missions'),
  getMission: (id: string) => request<Mission>('GET', `/missions/${id}`),
  runMissionStep: (id: string) =>
    request<MissionStepRunResponse>('POST', `/missions/${id}/step`, undefined, SLOW_TIMEOUT_MS),
  getMissionSteps: (missionId?: string | null) =>
    request<MissionStep[]>('GET', `/mission-steps${query({ mission_id: missionId })}`),
  getMissionStep: (id: string) => request<MissionStep>('GET', `/mission-steps/${id}`),

  // Autonomous mission execution (Stage 12). Start returns after QUEUEING (not completion);
  // pause/resume/cancel act at atomic boundaries. Status/timeline reads NEVER start a
  // worker, execute a route, or call a model — they only read persisted state.
  startMission: (id: string) =>
    request<MissionStartResponse>('POST', `/missions/${id}/start`),
  pauseMission: (id: string) => request<MissionExecutionRun>('POST', `/missions/${id}/pause`),
  resumeMission: (id: string) => request<MissionExecutionRun>('POST', `/missions/${id}/resume`),
  cancelMission: (id: string) => request<MissionExecutionRun>('POST', `/missions/${id}/cancel`),
  runMissionAutoStep: (id: string) =>
    request<MissionExecutionStatus>('POST', `/missions/${id}/run-step`, undefined, SLOW_TIMEOUT_MS),
  getMissionExecution: (id: string) =>
    request<MissionExecutionStatus>('GET', `/missions/${id}/execution`),
  getMissionTimeline: (id: string) =>
    request<MissionTimeline>('GET', `/missions/${id}/timeline`),
  getMissionRuns: () => request<MissionExecutionRun[]>('GET', '/mission-runs'),
  getMissionRun: (id: string) => request<MissionExecutionRun>('GET', `/mission-runs/${id}`),
  getMissionWorkerStatus: () => request<MissionWorkerStatus>('GET', '/mission-worker/status'),

  // Events
  getEvents: (missionId?: string | null) =>
    request<NodeEvent[]>('GET', `/events${query({ mission_id: missionId })}`),

  // External agents
  connectAgent: (body: ExternalAgentCreate) =>
    request<ExternalAgent>('POST', '/agents/connect', body),
  getAgents: () => request<ExternalAgent[]>('GET', '/agents'),
  getAgentsStatus: () => request<ExternalAgentStatusSnapshot>('GET', '/agents/status'),
  getAgent: (id: string) => request<ExternalAgent>('GET', `/agents/${id}`),
  setAgentStatus: (id: string, status: ExternalAgentStatusValue) =>
    request<ExternalAgent>('PATCH', `/agents/${id}/status`, { status }),
  sendAgentMessage: (id: string, body: ExternalAgentMessageCreate) =>
    request<ExternalAgentMessageResponse>(
      'POST',
      `/agents/${id}/message`,
      body,
      // run_step can trigger a coordinator/coding step, so allow the slow timeout.
      SLOW_TIMEOUT_MS,
    ),
  getAgentMessages: (id: string) =>
    request<ExternalAgentMessage[]>('GET', `/agents/${id}/messages`),
  getAllAgentMessages: (params: { agent_id?: string | null; mission_id?: string | null } = {}) =>
    request<ExternalAgentMessage[]>('GET', `/agent-messages${query(params)}`),

  // Stage 13A: authenticated agent transport (identity, peers, durable outbox/inbox).
  // Read methods never start a send — only POSTs to messages/test/refresh do.
  getIdentityStatus: () => request<IdentityStatus>('GET', '/identity/status'),
  initializeIdentity: () => request<IdentityStatus>('POST', '/identity/initialize'),
  getTransportStatus: () => request<TransportStatus>('GET', '/agent-transport/status'),
  getPeers: () => request<Peer[]>('GET', '/peers'),
  createPeer: (body: PeerCreate) => request<Peer>('POST', '/peers', body),
  trustPeer: (id: string, body: { public_key?: string | null; fingerprint?: string | null } = {}) =>
    request<Peer>('POST', `/peers/${id}/trust`, body),
  revokePeer: (id: string) => request<Peer>('POST', `/peers/${id}/revoke`),
  enablePeer: (id: string) => request<Peer>('POST', `/peers/${id}/enable`),
  disablePeer: (id: string) => request<Peer>('POST', `/peers/${id}/disable`),
  testPeer: (id: string) =>
    request<{ ok: boolean }>('POST', `/peers/${id}/test`, undefined, SLOW_TIMEOUT_MS),
  refreshPeerManifest: (id: string) =>
    request<Record<string, unknown>>('POST', `/peers/${id}/refresh-manifest`, undefined, SLOW_TIMEOUT_MS),
  getOutbox: () => request<OutboxMessage[]>('GET', '/agent-transport/outbox'),
  getInbox: () => request<InboxMessage[]>('GET', '/agent-transport/inbox'),
  sendAgentTransportMessage: (body: AgentMessageSendRequest) =>
    request<AgentMessageSendResponse>('POST', '/agent-transport/messages', body, SLOW_TIMEOUT_MS),
  retryAgentTransportMessage: (id: string) =>
    request<OutboxMessage>('POST', `/agent-transport/messages/${id}/retry`),
  cancelAgentTransportMessage: (id: string) =>
    request<OutboxMessage>('POST', `/agent-transport/messages/${id}/cancel`),

  // Stage 13A.5: RF backends. Read methods never start a subprocess or a tool call —
  // only start/stop/restart/refresh/call POSTs do.
  getRFBackends: () => request<RFBackendStatus[]>('GET', '/rf/backends'),
  startRFBackend: (id: string) =>
    request<RFBackendStatus>('POST', `/rf/backends/${id}/start`, undefined, SLOW_TIMEOUT_MS),
  stopRFBackend: (id: string) =>
    request<RFBackendStatus>('POST', `/rf/backends/${id}/stop`, undefined, SLOW_TIMEOUT_MS),
  restartRFBackend: (id: string) =>
    request<RFBackendStatus>('POST', `/rf/backends/${id}/restart`, undefined, SLOW_TIMEOUT_MS),
  getRFTools: (id: string) => request<RFToolInfo[]>('GET', `/rf/backends/${id}/tools`),
  refreshRFTools: (id: string) =>
    request<RFToolInfo[]>('POST', `/rf/backends/${id}/tools/refresh`, undefined, SLOW_TIMEOUT_MS),
  getRFContexts: () => request<RFContext[]>('GET', '/rf/contexts'),
  refreshRFContext: (id: string) =>
    request<RFContext>('POST', `/rf/contexts/${id}/refresh`, undefined, SLOW_TIMEOUT_MS),
  getRFArtifacts: (backendId?: string) =>
    request<RFArtifact[]>('GET', `/rf/artifacts${query({ backend_id: backendId })}`),

  // Stage 13B: coordinator communications. All read endpoints; PATCH permissions is the
  // only mutation. Reads never send a message or resume a mission.
  getConversations: () => request<ConversationSummary[]>('GET', '/conversations'),
  getInboundRequests: () => request<InboundRequest[]>('GET', '/inbound-requests'),
  getMissionCommunications: (missionId: string) =>
    request<MissionCommunications>('GET', `/missions/${missionId}/communications`),
  getMissionReplyWaits: (missionId: string) =>
    request<ReplyWait[]>('GET', `/missions/${missionId}/reply-waits`),
  cancelReplyWait: (missionId: string, waitId: string) =>
    request<{ cancelled: boolean; wait_id: string }>(
      'POST',
      `/missions/${missionId}/reply-waits/${waitId}/cancel`,
    ),
  getPeerPermissions: (peerId: string) =>
    request<PeerPermissions>('GET', `/peers/${peerId}/permissions`),
  setPeerPermissions: (peerId: string, body: Partial<PeerPermissions>) =>
    request<PeerPermissions>('PATCH', `/peers/${peerId}/permissions`, body),

  // Stage 13C: operator-grade aggregate read models + the operator composer. All reads are
  // side-effect free (no send/resume). `sendOperatorMessage` is the ONLY mutation: it queues
  // ONE message through the durable outbox — the browser never contacts a peer directly.
  getOverview: () => request<Overview>('GET', '/overview'),
  getFleet: () => request<Fleet>('GET', '/fleet'),
  getConversationTimeline: (conversationId: string) =>
    request<ConversationTimeline>('GET', `/conversations/${conversationId}/timeline`),
  getDistributedMissionTimeline: (missionId: string) =>
    request<DistributedMissionTimeline>('GET', `/missions/${missionId}/distributed-timeline`),
  getCommunicationsSummary: () =>
    request<CommunicationsSummary>('GET', '/communications/summary'),
  getReplyWaits: (state?: string) =>
    request<ReplyWait[]>('GET', `/reply-waits${query({ state })}`),
  sendOperatorMessage: (body: OperatorMessageRequest) =>
    request<OperatorMessageResponse>('POST', '/communications/send', body),

  // Stage 13D.2: cross-node artifact transfer. Reads are side-effect free. offer/request queue
  // ONE signed control message via the durable transport — the browser never contacts a peer's
  // binary endpoint, and no response ever carries bytes, absolute paths, keys, or signatures.
  getArtifacts: () => request<ArtifactSummary[]>('GET', '/artifacts'),
  getRemoteArtifactOffers: () => request<RemoteArtifactOffer[]>('GET', '/remote-artifact-offers'),
  getArtifactTransfers: () => request<ArtifactTransfer[]>('GET', '/artifact-transfers'),
  getArtifactStoreStatus: () => request<ArtifactStoreStatus>('GET', '/artifact-store/status'),
  getArtifactPermissions: (peerId: string) =>
    request<ArtifactPermissions>('GET', `/peers/${peerId}/artifact-permissions`),
  setArtifactPermissions: (peerId: string, body: Partial<ArtifactPermissions>) =>
    request<ArtifactPermissions>('PATCH', `/peers/${peerId}/artifact-permissions`, body),
  offerArtifact: (body: { artifact_id: string; peer_id: string }) =>
    request<{ transfer_id: string; state: string }>('POST', '/artifact-transfers/offer', body),
  requestArtifact: (body: { peer_id: string; origin_artifact_id: string }) =>
    request<{ transfer_id: string; state: string }>('POST', '/artifact-transfers/request', body),
  cancelArtifactTransfer: (id: string) =>
    request<{ cancelled: boolean }>('POST', `/artifact-transfers/${id}/cancel`),
  retryArtifactTransfer: (id: string) =>
    request<{ retried: boolean }>('POST', `/artifact-transfers/${id}/retry`),
  pinArtifact: (id: string) => request<ArtifactSummary>('POST', `/artifacts/${id}/pin`),
  unpinArtifact: (id: string) => request<ArtifactSummary>('POST', `/artifacts/${id}/unpin`),
  artifactStoreGc: (dryRun: boolean) =>
    request<{ dry_run: boolean; removed_objects: number; reclaimed_bytes: number }>(
      'POST', '/artifact-store/gc', { dry_run: dryRun },
    ),

  // Stage 13D.3: authenticated remote mission-status. Reads are side-effect free; the only
  // mutation is a bounded status query for a KNOWN snapshot (no arbitrary target/enumeration).
  getRemoteMissions: (peerId?: string) =>
    request<RemoteMissionSnapshot[]>('GET', `/remote-missions${query({ peer_id: peerId })}`),
  getRemoteMission: (id: string) =>
    request<RemoteMissionSnapshot>('GET', `/remote-missions/${id}`),
  getRemoteMissionEvents: (id: string) =>
    request<RemoteMissionStatusEvent[]>('GET', `/remote-missions/${id}/events`),
  queryRemoteMission: (id: string) =>
    request<{ queued: boolean; snapshot_id: string }>('POST', `/remote-missions/${id}/query`),
  getMissionStatusPublications: (missionId: string) =>
    request<MissionStatusPublication[]>('GET', `/missions/${missionId}/status-publications`),
  getMissionStatusPermissions: (peerId: string) =>
    request<MissionStatusPermissions>('GET', `/peers/${peerId}/mission-status-permissions`),
  setMissionStatusPermissions: (peerId: string, body: Partial<MissionStatusPermissions>) =>
    request<MissionStatusPermissions>(
      'PATCH', `/peers/${peerId}/mission-status-permissions`, body,
    ),

  // Stage 14A: read-only operations metadata (version, readiness, migrations, storage, backups,
  // preflight). Destructive restore/upgrade actions are CLI-only and never exposed here.
  getOpsOverview: () => request<OpsOverview>('GET', '/ops/overview'),
  getOpsPreflight: () => request<OpsPreflight>('GET', '/ops/preflight'),
  getOpsBackups: () => request<OpsBackup[]>('GET', '/ops/backups'),

  // Stage 14B: managed SDR hardware. Read endpoints are always available; lease actions and
  // enable/disable require the node's operator_lease_actions_enabled flag (else HTTP 403).
  getHardwareStatus: () => request<HardwareStatus>('GET', '/hardware/status'),
  getHardwareProviders: () => request<HardwareProvider[]>('GET', '/hardware/providers'),
  getHardwareDevices: () => request<HardwareDevice[]>('GET', '/hardware/devices'),
  getHardwareDevice: (id: string) => request<HardwareDevice>('GET', `/hardware/devices/${id}`),
  getHardwareCapabilities: (id: string) =>
    request<HardwareCapability>('GET', `/hardware/devices/${id}/capabilities`),
  getHardwareHealthEvents: (id: string) =>
    request<HardwareHealthEvent[]>('GET', `/hardware/devices/${id}/health-events`),
  refreshHardware: () =>
    request<HardwareRefreshResult>('POST', '/hardware/refresh', {}, SLOW_TIMEOUT_MS),
  getHardwareLeases: (active = false) =>
    request<HardwareLease[]>('GET', `/hardware/leases${query({ active })}`),
  acquireHardwareLease: (body: HardwareLeaseAcquire) =>
    request<HardwareLease>('POST', '/hardware/leases/acquire', body),
  releaseHardwareLease: (id: string) =>
    request<HardwareLease>('POST', `/hardware/leases/${id}/release`, {}),
  revokeHardwareLease: (id: string) =>
    request<HardwareLease>('POST', `/hardware/leases/${id}/revoke`, {}),
  setHardwareDeviceEnabled: (id: string, enabled: boolean) =>
    request<HardwareDevice>('POST', `/hardware/devices/${id}/${enabled ? 'enable' : 'disable'}`, {}),

  // Stage 14C.1: real-hardware qualification (read-only surfaces).
  getHardwareSupportMatrix: () =>
    request<HardwareSupportEntry[]>('GET', '/hardware/support-matrix'),
  getHardwareQualifications: () =>
    request<HardwareQualificationRun[]>('GET', '/hardware/qualifications'),
  getHardwareQualificationChecks: (id: string) =>
    request<HardwareQualificationCheck[]>('GET', `/hardware/qualifications/${id}/checks`),
  getHardwareMeasurements: () =>
    request<HardwareMeasurement[]>('GET', '/hardware/measurements'),

  // Stage 14C.2: field-validation campaigns (read-only surfaces).
  getFieldCampaigns: () => request<FieldCampaign[]>('GET', '/field/campaigns'),
  getFieldCampaign: (id: string) => request<FieldCampaign>('GET', `/field/campaigns/${id}`),
  getFieldCampaignChecks: (id: string) =>
    request<FieldCheck[]>('GET', `/field/campaigns/${id}/checks`),
  getFieldCampaignReport: (id: string) =>
    request<Record<string, unknown>>('GET', `/field/campaigns/${id}/report`),

  // Stage 14D: external-agent interoperability (read-only operator surfaces; no secrets).
  getExternalAgents: () => request<InteropAgent[]>('GET', '/external-agents'),
  getExternalAgentsDiagnostics: () =>
    request<InteropDiagnostics>('GET', '/external-agents/diagnostics'),
  getExternalAgentEndpoints: (id: string) =>
    request<InteropEndpoint[]>('GET', `/external-agents/${id}/endpoints`),
  getExternalAgentSubscriptions: (id: string) =>
    request<InteropSubscription[]>('GET', `/external-agents/${id}/subscriptions`),
  getExternalAgentDeliveries: (id: string) =>
    request<InteropDelivery[]>('GET', `/external-agents/${id}/deliveries`),

  // Stage 14E: data & privacy (read-only operator surfaces; no secrets/tokens/keys/paths).
  getDataConsent: () => request<DataConsentView>('GET', '/data/consent'),
  getDataDiagnostics: () => request<DataDiagnostics>('GET', '/data/diagnostics'),
  getDataRecords: (category?: string, status?: string) => {
    const q = new URLSearchParams()
    if (category) q.set('category', category)
    if (status) q.set('status', status)
    const qs = q.toString()
    return request<DataRecord[]>('GET', `/data/records${qs ? `?${qs}` : ''}`)
  },
  getDataDestinations: () => request<DataDestination[]>('GET', '/data/destinations'),
  getDataBatches: () => request<DataBatch[]>('GET', '/data/batches'),
  getDataDeletions: () => request<DataDeletion[]>('GET', '/data/deletions'),
  getDataDatasets: () => request<DataDataset[]>('GET', '/data/datasets'),
}
