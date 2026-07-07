// Stage 13D.3 frontend test: the Remote Missions view. The `api` client is fully mocked — no
// backend/network is contacted. Asserts the UI renders authenticated remote state + freshness
// honestly (text, not colour alone), that the bounded status-query action calls the API (the
// browser never contacts a peer), that reads cause no writes, and that no secrets are shown.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => {
  const api = {
    baseUrl: 'http://test',
    getRemoteMissions: vi.fn(),
    getRemoteMission: vi.fn(),
    getRemoteMissionEvents: vi.fn(),
    queryRemoteMission: vi.fn(),
  }
  class ApiError extends Error {
    category = 'http'
  }
  return { api, ApiError, API_BASE_URL: 'http://test' }
})

import { api } from '../api/client'
import { RemoteMissionsPage } from './RemoteMissionsPage'

const SNAPSHOT = {
  snapshot_id: 'snap-12345678',
  origin_node_id: 'node-b',
  peer_id: 'peer-12345678',
  remote_mission_ref: 'rm-12345678',
  request_id: 'req-12345678',
  parent_request_id: null,
  conversation_id: 'conv-12345678',
  local_mission_id: 'mission-12345678',
  latest_sequence: 3,
  state: 'waiting',
  response_obligation: 'required',
  progress_summary: null,
  terminal_category: null,
  freshness: 'fresh' as const,
  artifact_count: 1,
  transfer_count: 1,
  artifact_ids: ['art-1'],
  transfer_ids: ['xfer-1'],
  remote_updated_at: '2026-06-14T00:00:00Z',
  received_at: '2026-06-14T00:00:01Z',
  freshness_deadline: '2026-06-14T00:05:00Z',
  query_pending: false,
  protocol_version: '1',
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(api.getRemoteMissions as ReturnType<typeof vi.fn>).mockResolvedValue([SNAPSHOT])
  ;(api.queryRemoteMission as ReturnType<typeof vi.fn>).mockResolvedValue({
    queued: true,
    snapshot_id: SNAPSHOT.snapshot_id,
  })
  ;(api.getRemoteMissionEvents as ReturnType<typeof vi.fn>).mockResolvedValue([
    { event_id: 'e1', sequence: 3, state: 'waiting', message_type: 'status_update',
      disposition: 'accepted', reason: null, peer_id: SNAPSHOT.peer_id,
      received_at: '2026-06-14T00:00:01Z' },
  ])
})

describe('RemoteMissionsPage', () => {
  it('renders authenticated remote state with a textual freshness label', async () => {
    render(<RemoteMissionsPage />)
    expect(await screen.findByText('waiting')).toBeTruthy()
    // freshness is a text label, not colour alone
    expect(screen.getByText(/freshness: fresh/)).toBeTruthy()
    expect(screen.getByText(/seq 3/)).toBeTruthy()
  })

  it('shows an unknown/empty state when there are no snapshots', async () => {
    (api.getRemoteMissions as ReturnType<typeof vi.fn>).mockResolvedValue([])
    render(<RemoteMissionsPage />)
    expect(await screen.findByText(/No remote-mission snapshots yet/)).toBeTruthy()
  })

  it('queues a status query through the backend (never contacts a peer directly)', async () => {
    render(<RemoteMissionsPage />)
    const button = await screen.findByLabelText(/Query latest status/)
    fireEvent.click(button)
    await waitFor(() =>
      expect(api.queryRemoteMission).toHaveBeenCalledWith(SNAPSHOT.snapshot_id),
    )
  })

  it('initial render performs only reads (no query/mutation)', async () => {
    render(<RemoteMissionsPage />)
    await screen.findByText('waiting')
    expect(api.getRemoteMissions).toHaveBeenCalled()
    expect(api.queryRemoteMission).not.toHaveBeenCalled()
  })

  it('does not render secrets, signatures, or envelopes', async () => {
    const { container } = render(<RemoteMissionsPage />)
    await screen.findByText('waiting')
    const text = container.textContent?.toLowerCase() ?? ''
    expect(text).not.toContain('signature')
    expect(text).not.toContain('-----begin')
    expect(text).not.toContain('envelope')
  })
})
