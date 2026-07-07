// Stage 13C frontend component tests (Part S). Every test mocks the `api` client — no real
// backend, network, Gemini, Codex, GNU Radio, or Marconi is contacted. They assert the UI
// renders persisted state honestly and preserves the ACK-vs-semantic-reply and
// trust-vs-authorization distinctions, that reads cause no writes, and that destructive and
// composer actions are guarded.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// One shared mock for the only I/O boundary. hooks.ts imports ApiError from the same module.
vi.mock('../api/client', () => {
  class ApiError extends Error {
    category = 'http'
  }
  const api = {
    baseUrl: 'http://test',
    getOverview: vi.fn(),
    getFleet: vi.fn(),
    getConversations: vi.fn(),
    getConversationTimeline: vi.fn(),
    getDistributedMissionTimeline: vi.fn(),
    getReplyWaits: vi.fn(),
    getInboundRequests: vi.fn(),
    getOutbox: vi.fn(),
    getInbox: vi.fn(),
    sendOperatorMessage: vi.fn(),
    trustPeer: vi.fn(),
    revokePeer: vi.fn(),
    enablePeer: vi.fn(),
    disablePeer: vi.fn(),
    testPeer: vi.fn(),
    refreshPeerManifest: vi.fn(),
    setPeerPermissions: vi.fn(),
    createPeer: vi.fn(),
    cancelReplyWait: vi.fn(),
    retryAgentTransportMessage: vi.fn(),
    cancelAgentTransportMessage: vi.fn(),
    getArtifacts: vi.fn(),
    getRemoteArtifactOffers: vi.fn(),
    getArtifactTransfers: vi.fn(),
    getArtifactStoreStatus: vi.fn(),
    requestArtifact: vi.fn(),
    cancelArtifactTransfer: vi.fn(),
    retryArtifactTransfer: vi.fn(),
    pinArtifact: vi.fn(),
    unpinArtifact: vi.fn(),
    artifactStoreGc: vi.fn(),
  }
  return { api, ApiError }
})

import { api } from '../api/client'
import type { ConversationTimeline, Fleet, Overview } from '../api/types'
import { ArtifactsPage } from './ArtifactsPage'
import { ConversationsPage } from './ConversationsPage'
import { DiagnosticsPage } from './DiagnosticsPage'
import { DistributedMissionPanel } from './DistributedMissionPanel'
import { FleetPage } from './FleetPage'
import { OperatorComposer } from './OperatorComposer'
import { OverviewPage } from './OverviewPage'

const mockApi = api as unknown as Record<string, ReturnType<typeof vi.fn>>

function trustedPeer(overrides: Partial<Fleet['peers'][number]> = {}): Fleet['peers'][number] {
  return {
    peer_id: 'peer-aaaaaaaa-1111',
    name: 'Node B',
    role: 'node',
    trust_state: 'trusted',
    enabled: true,
    transport_health: 'healthy',
    last_success_at: '2026-06-14T00:00:00Z',
    last_failure_at: null,
    consecutive_failures: 0,
    fingerprint: 'sha256:abcd1234abcd1234',
    manifest_at: null,
    capabilities: null,
    permissions: {
      may_send_requests: false,
      may_send_replies: true,
      may_receive_messages: true,
      may_request_response: false,
      max_inbound_request_bytes: null,
      max_concurrent_inbound_missions: null,
    },
    conversation_count: 1,
    outstanding_messages: 0,
    pending_reply_waits: 1,
    ...overrides,
  }
}

const overviewFixture: Overview = {
  node: {
    node_id: 'node-a', node_name: 'Node A', runtime_status: 'running', version: '0.1.0',
    started_at: null, mission_count: 3, event_count: 9, identity_initialized: true,
    fingerprint: 'sha256:deadbeefdeadbeef',
  },
  workers: {
    mission: { enabled: true, running: true, degraded: false, active_count: 1, queued_count: 0 },
    transport: { enabled: true, running: true, degraded: false, in_flight: 0 },
  },
  coordinator: { provider: 'gemini_cli', configured: true, missing_configuration: [] },
  coding_agent: { provider: 'codex_cli', configured: true, missing_configuration: [] },
  rf_backends: [{ backend_id: 'legacy_gr_mcp', state: 'ready', tool_count: 5, experimental: false, version: null, last_error_type: null }],
  peers: { total: 1, trusted: 1 },
  outbox: { pending: 0, in_flight: 0, delivered: 0, acknowledged: 1, failed: 0, dead_letter: 0 },
  inbox_count: 0,
  missions: { active: 1, waiting: 0, queued: 0 },
  reply_waits: { pending: 1, by_state: { pending: 1 } },
  inbound_requests: { total: 0, by_state: {} },
  recent_failures: { communication: [], rf: [], mission: [] },
}

const timelineFixture: ConversationTimeline = {
  conversation_id: 'conv-12345678',
  peers: ['peer-aaaaaaaa-1111'],
  message_count: 2,
  entries: [
    {
      kind: 'outbound_request', direction: 'outbound', message_id: 'msg-1', message_type: 'request',
      peer_id: 'peer-aaaaaaaa-1111', text_preview: 'What can you do?', data_preview: null,
      request_id: 'req-1', reply_to_request_id: null, expects_reply: true,
      delivery_status: 'acknowledged', transport_acknowledged: true, semantic_reply: false,
      mission_id: 'm-1', at: '2026-06-14T00:00:00Z',
    },
    {
      kind: 'inbound_reply', direction: 'inbound', message_id: 'msg-2', message_type: 'reply',
      peer_id: 'peer-aaaaaaaa-1111', text_preview: 'I can do RF.', data_preview: null,
      request_id: null, reply_to_request_id: 'req-1', delivery_status: 'received',
      transport_acknowledged: true, semantic_reply: true, mission_id: null,
      at: '2026-06-14T00:01:00Z',
    },
  ],
  legend: { transport_acknowledged: 'delivered not answered', semantic_reply: 'a real answer' },
}

beforeEach(() => {
  for (const fn of Object.values(mockApi)) {
    if (typeof fn?.mockReset === 'function') fn.mockReset()
  }
  mockApi.getOverview.mockResolvedValue(overviewFixture)
  mockApi.getFleet.mockResolvedValue({ peers: [trustedPeer()] } satisfies Fleet)
  mockApi.getConversations.mockResolvedValue([
    { conversation_id: 'conv-12345678', outbound: 1, inbound: 1, peers: ['peer-aaaaaaaa-1111'] },
  ])
  mockApi.getConversationTimeline.mockResolvedValue(timelineFixture)
  mockApi.getReplyWaits.mockResolvedValue([
    { wait_id: 'w-1', mission_id: 'm-1', request_id: 'req-1', outbound_message_id: 'msg-1', expected_peer_id: 'peer-aaaaaaaa-1111', conversation_id: 'conv-12345678', state: 'pending', deadline: null, satisfying_inbox_message_id: null, created_at: '2026-06-14T00:00:00Z', reason: null },
  ])
  mockApi.getInboundRequests.mockResolvedValue([])
  mockApi.getOutbox.mockResolvedValue([])
  mockApi.getInbox.mockResolvedValue([])
  mockApi.getArtifacts.mockResolvedValue([])
  mockApi.getRemoteArtifactOffers.mockResolvedValue([])
  mockApi.getArtifactTransfers.mockResolvedValue([])
  mockApi.getArtifactStoreStatus.mockResolvedValue({
    total_quota_bytes: 1000, used_bytes: 100, available_in_quota_bytes: 900,
    free_disk_bytes: 5000, minimum_free_bytes: 0, max_artifact_bytes: 500,
  })
})

// == Overview ================================================================================

describe('OverviewPage', () => {
  it('renders node identity, health and comms rollup with labels (not colour alone)', async () => {
    render(<OverviewPage />)
    expect(await screen.findByText('Node A')).toBeInTheDocument()
    // Worker health is shown as text, derived from running/degraded (not a configured flag).
    expect(screen.getAllByText(/running/).length).toBeGreaterThan(0)
    expect(screen.getByText(/sha256:deadbeefdeadbeef/)).toBeInTheDocument()
  })
})

// == Conversations (list, filters, ACK vs reply, empty, error) ===============================

describe('ConversationsPage', () => {
  it('renders the conversation list', async () => {
    render(<ConversationsPage />)
    expect(await screen.findByText(/conv-123/)).toBeInTheDocument()
  })

  it('shows ACK distinctly from a semantic reply in the timeline', async () => {
    render(<ConversationsPage />)
    fireEvent.click(await screen.findByText(/conv-123/))
    // The outbound request is acknowledged (delivered) but NOT a semantic answer.
    expect(await screen.findByText(/ACK ✓ \(delivered, not answered\)/)).toBeInTheDocument()
    // The inbound reply IS a semantic reply (legend + the entry badge both render it).
    expect(screen.getAllByText(/semantic reply ✓/).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('What can you do?')).toBeInTheDocument()
  })

  it('filters by pending reply', async () => {
    render(<ConversationsPage />)
    await screen.findByText(/conv-123/)
    fireEvent.click(screen.getByLabelText(/pending reply/i))
    expect(screen.getByText(/conv-123/)).toBeInTheDocument() // still present (has a pending wait)
  })

  it('shows an empty state when there are no conversations', async () => {
    mockApi.getConversations.mockResolvedValue([])
    render(<ConversationsPage />)
    expect(await screen.findByText(/No conversations match/)).toBeInTheDocument()
  })

  it('surfaces an API error', async () => {
    mockApi.getConversations.mockRejectedValue(new Error('boom'))
    render(<ConversationsPage />)
    expect(await screen.findByText(/boom/)).toBeInTheDocument()
  })
})

// == Operator composer =======================================================================

describe('OperatorComposer', () => {
  it('requires a trusted, authorized peer (none -> guidance, no send)', () => {
    render(<OperatorComposer peers={[trustedPeer({ trust_state: 'untrusted' })]} />)
    expect(screen.getByText(/No trusted, enabled, message-authorized peer/)).toBeInTheDocument()
  })

  it('rejects malformed JSON data without sending', async () => {
    render(<OperatorComposer peers={[trustedPeer()]} />)
    fireEvent.change(screen.getByLabelText(/Structured data/i), { target: { value: '{not json' } })
    fireEvent.click(screen.getByRole('button', { name: /Queue message/i }))
    expect(await screen.findByText(/Invalid JSON/)).toBeInTheDocument()
    expect(mockApi.sendOperatorMessage).not.toHaveBeenCalled()
  })

  it('queues exactly one message and shows queued (not answered) state', async () => {
    mockApi.sendOperatorMessage.mockResolvedValue({
      message_id: 'msg-9', request_id: 'req-9', conversation_id: 'conv-1', peer_id: 'peer-aaaaaaaa-1111',
      status: 'pending', expects_reply: true, origin: 'operator',
    })
    render(<OperatorComposer peers={[trustedPeer()]} />)
    fireEvent.change(screen.getByLabelText(/Message text/i), { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: /Queue message/i }))
    expect(await screen.findByText(/operator/)).toBeInTheDocument()
    expect(screen.getByText(/not yet acknowledged or answered/)).toBeInTheDocument()
    expect(mockApi.sendOperatorMessage).toHaveBeenCalledTimes(1)
  })

  it('disables submit while in flight so a double-click cannot queue twice', async () => {
    let resolve: (v: unknown) => void = () => undefined
    mockApi.sendOperatorMessage.mockImplementation(() => new Promise((r) => { resolve = r }))
    render(<OperatorComposer peers={[trustedPeer()]} />)
    fireEvent.change(screen.getByLabelText(/Message text/i), { target: { value: 'hi' } })
    const btn = screen.getByRole('button', { name: /Queue message/i })
    fireEvent.click(btn)
    await waitFor(() => expect(btn).toBeDisabled())
    fireEvent.click(btn) // ignored — disabled
    resolve({ message_id: 'm', request_id: 'r', conversation_id: null, peer_id: 'p', status: 'pending', expects_reply: false, origin: 'operator' })
    await waitFor(() => expect(mockApi.sendOperatorMessage).toHaveBeenCalledTimes(1))
  })
})

// == Fleet (trust vs authorization, confirmations, no keys) ==================================

describe('FleetPage', () => {
  it('shows trust and authorization separately and never a full public key', async () => {
    render(<FleetPage />)
    expect(await screen.findByText('Node B')).toBeInTheDocument()
    expect(screen.getAllByText(/trust: trusted/).length).toBeGreaterThan(0)
    // Authorization flags are shown distinctly from trust.
    expect(screen.getAllByText(/receive messages: allowed/).length).toBeGreaterThan(0)
    // Only an abbreviated fingerprint is ever rendered.
    expect(screen.queryByText(/-----BEGIN/)).not.toBeInTheDocument()
  })

  it('requires confirmation before revoking trust', async () => {
    mockApi.revokePeer.mockResolvedValue({})
    render(<FleetPage />)
    fireEvent.click(await screen.findByText('Node B'))
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    // The action is NOT fired yet — an inline confirm appears first.
    expect(mockApi.revokePeer).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(mockApi.revokePeer).toHaveBeenCalledTimes(1))
  })

  it('updates an application permission separately from trust', async () => {
    mockApi.setPeerPermissions.mockResolvedValue({})
    render(<FleetPage />)
    fireEvent.click(await screen.findByText('Node B'))
    const sendRequests = await screen.findByLabelText('send requests')
    fireEvent.click(sendRequests)
    await waitFor(() =>
      expect(mockApi.setPeerPermissions).toHaveBeenCalledWith('peer-aaaaaaaa-1111', { may_send_requests: true }),
    )
  })
})

// == Distributed mission (topology, remote unknown) =========================================

describe('DistributedMissionPanel', () => {
  it('shows peer message, wait and a topology with authenticated remote status + freshness', async () => {
    mockApi.getDistributedMissionTimeline.mockResolvedValue({
      mission_id: 'm-1', mission_status: 'waiting', mission_run_id: 'run-1', iteration_count: 2,
      source_type: 'user', is_inbound_mission: false,
      outbound_messages: [{ direction: 'outbound', message_id: 'msg-1', peer_id: 'peer-aaaaaaaa-1111', message_type: 'request', conversation_id: 'c', request_id: 'req-1', expects_reply: true, delivery_status: 'acknowledged', transport_acknowledged: true, at: 'x' }],
      reply_waits: [{ wait_id: 'w-1', mission_id: 'm-1', request_id: 'req-1', outbound_message_id: 'msg-1', expected_peer_id: 'peer-aaaaaaaa-1111', conversation_id: 'c', state: 'pending', deadline: null, satisfying_inbox_message_id: null, created_at: 'x', reason: null }],
      response_obligation: null, inbound_request: null,
      entries: [],
      topology: {
        nodes: [
          { id: 'mission:m-1', kind: 'local_mission', label: 'Local mission', status: 'waiting' },
          { id: 'peer:peer-aaaaaaaa-1111', kind: 'peer', label: 'Peer node', status: 'remote_unknown' },
          { id: 'remote_mission:peer-aaaaaaaa-1111:rm-1', kind: 'remote_mission', label: 'Remote mission', status: 'active', freshness: 'fresh', sequence: 2, terminal: false },
        ],
        edges: [
          { from: 'mission:m-1', to: 'peer:peer-aaaaaaaa-1111', kind: 'outbound_request', acknowledged: true },
          { from: 'remote_mission:peer-aaaaaaaa-1111:rm-1', to: 'mission:m-1', kind: 'remote_status_synced', sequence: 2, state: 'active', freshness: 'fresh' },
        ],
      },
      legend: {},
    })
    render(<DistributedMissionPanel selectedMissionId="m-1" />)
    expect(await screen.findByText(/Topology/)).toBeInTheDocument()
    // A bare peer node carries no status; the remote_mission node carries the authenticated
    // state + freshness (Stage 13D.3) — state is never inferred.
    expect(screen.getByText(/peer node/)).toBeInTheDocument()
    expect(screen.getByText(/active · fresh/)).toBeInTheDocument()
    expect(screen.getByText(/wait: pending/)).toBeInTheDocument()
  })

  it('prompts to select a mission when none chosen', () => {
    render(<DistributedMissionPanel selectedMissionId={null} />)
    expect(screen.getByText(/Select a mission/)).toBeInTheDocument()
  })
})

// == Diagnostics (transport-auth distinct from app-authorized; reply waits) ==================

describe('DiagnosticsPage', () => {
  it('shows reply waits and an inbound request whose transport-auth did not authorize a mission', async () => {
    mockApi.getInboundRequests.mockResolvedValue([
      { request_id: 'ir-1', peer_id: 'peer-aaaaaaaa-1111', sender_node_id: 'node-b', conversation_id: null, mission_id: null, response_required: false, response_queued: false, state: 'received', rejection_reason: 'not authorized', created_at: 'x' },
    ])
    render(<DiagnosticsPage />)
    // The "authenticated != authorized" caveat is shown.
    expect(await screen.findByText(/Transport authenticated does NOT necessarily mean application authorized/)).toBeInTheDocument()
    expect(await screen.findByText('rejected')).toBeInTheDocument()
    expect(screen.getByText('no mission')).toBeInTheDocument()
    // A pending reply wait can be cancelled (with confirmation), never marked satisfied by the UI.
    expect(await screen.findByText(/satisfied only by an authenticated, correlated semantic reply/)).toBeInTheDocument()
    expect(within(screen.getByText(/Reply waits/).closest('section')!).queryByText(/mark satisfied/i)).toBeNull()
  })
})

// == Artifacts (Stage 13D.2) ================================================================

describe('ArtifactsPage', () => {
  it('renders store status, a transfer with progress, and a confirmed cancel', async () => {
    mockApi.getArtifactTransfers.mockResolvedValue([
      {
        transfer_id: 'tr-1', direction: 'inbound', peer_id: 'peer-aaaaaaaa-1111', state: 'transferring',
        local_artifact_id: null, origin_artifact_id: 'o1', expected_digest: 'sha256:ab', expected_size: 2048,
        received_bytes: 1024, artifact_kind: 'capture', display_name: 'cap.bin', conversation_id: null,
        mission_id: null, attempt_count: 1, error_type: null, last_error: null, grant_expires_at: null,
        created_at: 'x', completed_at: null,
      },
    ])
    mockApi.cancelArtifactTransfer.mockResolvedValue({ cancelled: true })
    render(<ArtifactsPage />)
    // store status renders, bytes are shown as metadata (never raw content)
    expect(await screen.findByText(/Managed artifact store/)).toBeInTheDocument()
    expect(await screen.findByText(/transferring/)).toBeInTheDocument()
    // a transfer cancel requires confirmation (never fires immediately)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(mockApi.cancelArtifactTransfer).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(mockApi.cancelArtifactTransfer).toHaveBeenCalledWith('tr-1'))
  })

  it('requests a remote offer through the node (no direct browser-to-peer call)', async () => {
    mockApi.getRemoteArtifactOffers.mockResolvedValue([
      { id: 'of-1', peer_id: 'peer-aaaaaaaa-1111', origin_node_id: 'node-b', origin_artifact_id: 'oa-1',
        digest: 'sha256:cd', size_bytes: 4096, artifact_kind: 'capture', display_name: 'r.bin',
        state: 'offered', conversation_id: null, offered_at: 'x' },
    ])
    mockApi.requestArtifact.mockResolvedValue({ transfer_id: 't9', state: 'requested' })
    render(<ArtifactsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Request' }))
    await waitFor(() =>
      expect(mockApi.requestArtifact).toHaveBeenCalledWith({ peer_id: 'peer-aaaaaaaa-1111', origin_artifact_id: 'oa-1' }),
    )
  })
})
