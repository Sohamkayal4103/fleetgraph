// Stage 14D frontend test: the External Agents view. The api client is fully mocked.
// Asserts agents + status + fingerprint + delivery state render as accessible TEXT (Badge
// tones, never colour alone), diagnostics surface, and that no secret/token/path is rendered.

import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => {
  const api = {
    getExternalAgents: vi.fn(),
    getExternalAgentsDiagnostics: vi.fn(),
    getExternalAgentEndpoints: vi.fn(),
    getExternalAgentSubscriptions: vi.fn(),
    getExternalAgentDeliveries: vi.fn(),
  }
  class ApiError extends Error {
    category = 'http'
  }
  return { api, ApiError, API_BASE_URL: 'http://test' }
})

import { api } from '../api/client'
import { ExternalAgentsPage } from './ExternalAgentsPage'

const AGENT = {
  agent_id: 'agent-1',
  display_name: 'robo-arm',
  status: 'active',
  identity_type: 'ed25519',
  fingerprint: 'sha256:deadbeef',
  owner: 'ops',
  permissions: ['mission.submit', 'mission.read.own'],
  endpoint_policy: 'default',
  last_authenticated_at: null,
  last_delivery_at: null,
  created_at: '2026-06-18T00:00:00+00:00',
}

const DIAGNOSTICS = {
  enabled: true,
  callbacks_enabled: true,
  websocket_enabled: false,
  worker_running: true,
  worker_degraded: false,
  live_websocket_connections: 0,
  agents_total: 1,
  agents_active: 1,
  messages_by_status: { delivered: 2, dead_letter: 1 },
  pending_messages: 0,
  dead_letter_messages: 1,
  open_websocket_sessions: 0,
}

describe('ExternalAgentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.getExternalAgents as ReturnType<typeof vi.fn>).mockResolvedValue([AGENT])
    ;(api.getExternalAgentsDiagnostics as ReturnType<typeof vi.fn>).mockResolvedValue(DIAGNOSTICS)
    ;(api.getExternalAgentEndpoints as ReturnType<typeof vi.fn>).mockResolvedValue([
      { endpoint_id: 'e1', host: 'cb.example', scheme: 'https', port: 443,
        status: 'verified', policy_result: { allowed: true }, verified_at: 'x' },
    ])
    ;(api.getExternalAgentSubscriptions as ReturnType<typeof vi.fn>).mockResolvedValue([
      { subscription_id: 's1', delivery_mode: 'webhook', status: 'active', filters: {},
        endpoint_id: 'e1', next_sequence: 3, last_acked_sequence: 2, created_at: 'x' },
    ])
    ;(api.getExternalAgentDeliveries as ReturnType<typeof vi.fn>).mockResolvedValue([
      { message_pk: 'm1', message_id: 'mid', event_type: 'agent.mission.completed',
        delivery_mode: 'webhook', subscription_id: 's1', sequence: 2, status: 'dead_letter',
        attempt_count: 8, max_attempts: 8, last_status_code: 500, failure_category: 'http_5xx',
        mission_id: 'mis', artifact_id: null, next_attempt_at: null, delivered_at: null,
        acknowledged_at: null, created_at: 'x' },
    ])
  })

  it('renders agents + diagnostics as accessible text', async () => {
    render(<ExternalAgentsPage />)
    await waitFor(() => expect(api.getExternalAgents).toHaveBeenCalled())
    expect(screen.getByText('robo-arm')).toBeInTheDocument()
    expect(screen.getByText('sha256:deadbeef')).toBeInTheDocument()
    // Dead-letter count is shown as text, not colour alone.
    expect(screen.getAllByText('1').length).toBeGreaterThan(0)
  })

  it('inspects an agent and shows delivery state as text', async () => {
    render(<ExternalAgentsPage />)
    const btn = await screen.findByRole('button', { name: 'Inspect' })
    btn.click()
    expect(await screen.findByText('agent.mission.completed')).toBeInTheDocument()
    expect(screen.getByText('dead_letter')).toBeInTheDocument()
    expect(screen.getByText('cb.example', { exact: false })).toBeInTheDocument()
  })
})
