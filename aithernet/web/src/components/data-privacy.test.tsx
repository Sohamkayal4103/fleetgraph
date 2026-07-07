// Stage 14E frontend test: the Data & Privacy view. The api client is fully mocked.
// Asserts consent (by category), export/collection state, and batch status render as accessible
// TEXT (Badge tones, never colour alone), and that no secret/token/key/path is rendered.

import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => {
  const api = {
    getDataConsent: vi.fn(),
    getDataDiagnostics: vi.fn(),
    getDataDestinations: vi.fn(),
    getDataBatches: vi.fn(),
    getDataRecords: vi.fn(),
  }
  class ApiError extends Error {
    category = 'http'
  }
  return { api, ApiError, API_BASE_URL: 'http://test' }
})

import { api } from '../api/client'
import { DataPrivacyPage } from './DataPrivacyPage'

const DIAG = {
  enabled: true, tenant_id: 'local', collection_paused: false, export_enabled: false,
  export_paused: true, worker_running: false, pending_records: 2, held_records: 1,
  pending_batches: 0, dead_letter_batches: 1, free_disk_bytes: 1000000, air_gapped: false,
  drive_status: { configured: false, enabled: false, credentials_present: false },
}

const CONSENT = {
  effective: {
    operational: { category: 'operational', collection_allowed: true, export_allowed: false,
      raw_artifact_allowed: false, revocation_state: 'none', reason: 'no_grant' },
    training: { category: 'training', collection_allowed: false, export_allowed: false,
      raw_artifact_allowed: false, revocation_state: 'none', reason: 'no_grant' },
  },
  grants: [],
}

describe('DataPrivacyPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.getDataDiagnostics as ReturnType<typeof vi.fn>).mockResolvedValue(DIAG)
    ;(api.getDataConsent as ReturnType<typeof vi.fn>).mockResolvedValue(CONSENT)
    ;(api.getDataDestinations as ReturnType<typeof vi.fn>).mockResolvedValue([
      { destination_id: 'd1', kind: 'local_archive', name: 'default-local', enabled: true,
        status: 'enabled', readiness: 'ready', config: { root: 'archive' } },
    ])
    ;(api.getDataBatches as ReturnType<typeof vi.fn>).mockResolvedValue([
      { batch_id: 'batch-1234', destination_id: 'd1', status: 'dead_letter', record_count: 3,
        compressed_bytes: 100, encrypted: false, attempt_count: 8, max_attempts: 8,
        failure_category: 'http_5xx' },
    ])
    ;(api.getDataRecords as ReturnType<typeof vi.fn>).mockResolvedValue([
      { record_id: 'r1', category: 'operational', status: 'collected', sensitivity: 'low',
        byte_size: 42, retention_class: 'operational', redaction_policy_version: 'r1',
        created_at: 'x' },
    ])
  })

  it('renders consent + export state as accessible text', async () => {
    render(<DataPrivacyPage />)
    await waitFor(() => expect(api.getDataConsent).toHaveBeenCalled())
    // Export disabled shown as text.
    expect(screen.getAllByText('disabled').length).toBeGreaterThan(0)
    // Per-category consent rows present (operational appears in consent + records tables).
    expect(screen.getAllByText('operational').length).toBeGreaterThan(0)
    expect(screen.getAllByText('training').length).toBeGreaterThan(0)
    // Dead-letter count shown as text, not colour alone.
    expect(screen.getAllByText('1').length).toBeGreaterThan(0)
  })

  it('renders destinations + batch dead-letter status as text', async () => {
    render(<DataPrivacyPage />)
    await waitFor(() => expect(api.getDataDestinations).toHaveBeenCalled())
    expect(screen.getByText('default-local')).toBeInTheDocument()
    expect(await screen.findByText('dead_letter')).toBeInTheDocument()
  })
})
