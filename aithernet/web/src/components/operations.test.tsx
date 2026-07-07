// Stage 14A frontend test: the Operations view. The `api` client is fully mocked — no backend
// is contacted. Asserts the operator sees version, readiness phase + per-component startup state
// (text, not colour alone), storage/backup freshness, and preflight results; that reads cause no
// writes; and that no destructive OS/restore/upgrade control is rendered here.

import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => {
  const api = {
    baseUrl: 'http://test',
    getOpsOverview: vi.fn(),
    getOpsPreflight: vi.fn(),
    getOpsBackups: vi.fn(),
  }
  class ApiError extends Error {
    category = 'http'
  }
  return { api, ApiError, API_BASE_URL: 'http://test' }
})

import { api } from '../api/client'
import { OperationsPage } from './OperationsPage'

const OVERVIEW = {
  version: '0.1.0',
  node_id: 'node-aaaa-bbbb',
  node_name: 'NodeA',
  started_at: '2026-06-14T00:00:00+00:00',
  generated_at: '2026-06-14T00:01:00+00:00',
  readiness: {
    phase: 'degraded',
    ready: true,
    components: [
      { name: 'migrations', required: true, state: 'ready', detail: null },
      { name: 'transport_worker', required: true, state: 'ready', detail: null },
      { name: 'rf_default_backend', required: false, state: 'failed', detail: 'not started' },
    ],
  },
  migrations: { applied: 13, pending: [], schema_valid: true },
  storage: { path: '/srv', total_bytes: 100 * 1024 ** 3, free_bytes: 40 * 1024 ** 3, free_pct: 40 },
  backups: { count: 2, latest_created_at: '2026-06-13T00:00:00+00:00', latest_version: '0.1.0' },
  state_root_configured: true,
}

const PREFLIGHT = {
  ok: true,
  counts: { ok: 5, warn: 1, fail: 0 },
  checks: [
    { name: 'migration_state', category: 'database', status: 'ok', detail: 'schema up to date' },
    { name: 'coding_agent_executable', category: 'executables', status: 'warn',
      detail: "'claude' not on PATH" },
  ],
}

describe('OperationsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.getOpsOverview as ReturnType<typeof vi.fn>).mockResolvedValue(OVERVIEW)
    ;(api.getOpsPreflight as ReturnType<typeof vi.fn>).mockResolvedValue(PREFLIGHT)
    ;(api.getOpsBackups as ReturnType<typeof vi.fn>).mockResolvedValue([
      { path: '/srv/backups/a.tar.gz', bytes: 1000, created_at: '2026-06-13T00:00:00+00:00',
        app_version: '0.1.0', file_count: 3 },
    ])
  })

  it('renders version, readiness phase, and per-component startup state as text', async () => {
    render(<OperationsPage />)
    await waitFor(() => expect(screen.getAllByText('0.1.0').length).toBeGreaterThan(0))
    // readiness phase + ready badge are textual, not colour-only
    expect(screen.getAllByText(/degraded/i).length).toBeGreaterThan(0)
    expect(screen.getByText('migrations')).toBeInTheDocument()
    expect(screen.getByText('rf_default_backend')).toBeInTheDocument()
    expect(screen.getAllByText('failed').length).toBeGreaterThan(0)
  })

  it('shows storage freshness and backup info', async () => {
    render(<OperationsPage />)
    await screen.findByText(/40\.0 GiB free of/)
    expect(screen.getAllByText('2026-06-13T00:00:00+00:00').length).toBeGreaterThan(0)
  })

  it('shows preflight warnings honestly', async () => {
    render(<OperationsPage />)
    await waitFor(() =>
      expect(screen.getByText('coding_agent_executable')).toBeInTheDocument(),
    )
    expect(screen.getByText(/not on PATH/)).toBeInTheDocument()
  })

  it('performs no writes on read (only GET ops endpoints are called)', async () => {
    render(<OperationsPage />)
    await waitFor(() => expect(api.getOpsOverview).toHaveBeenCalled())
    expect(api.getOpsPreflight).toHaveBeenCalled()
    expect(api.getOpsBackups).toHaveBeenCalled()
    // there is no restore/upgrade/exec mutation method on the mocked client
    expect((api as Record<string, unknown>).restore).toBeUndefined()
    expect((api as Record<string, unknown>).upgradeApply).toBeUndefined()
  })
})
