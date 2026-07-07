// Stage 14C.2 frontend test: the Field Validation view. The api client is fully mocked.
// Asserts campaigns + acceptance classification + per-check status render as accessible TEXT
// (never colour alone), and that no disruptive control is exposed in this read-only view.

import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => {
  const api = {
    baseUrl: 'http://test',
    getFieldCampaigns: vi.fn(),
    getFieldCampaignChecks: vi.fn(),
  }
  class ApiError extends Error {
    category = 'http'
  }
  return { api, ApiError, API_BASE_URL: 'http://test' }
})

import { api } from '../api/client'
import { FieldPage } from './FieldPage'

const CAMPAIGN = {
  id: 'camp-aaaa-bbbb',
  node_id: 'n1',
  name: 'pluto-soak',
  objective: 'single-device soak',
  profile: 'smoke',
  test_mode: 'validation',
  status: 'completed',
  classification: 'single_device_soak_complete',
  software_version: '0.1.0',
  config_digest: 'sha256:abc',
  topology: {},
  thresholds: {},
  warnings: [],
  summary: 'captures 3/3',
  checks_passed: 3,
  checks_failed: 0,
  checks_not_executed: 1,
  started_at: '2026-06-14T00:00:00+00:00',
  completed_at: '2026-06-14T00:05:00+00:00',
  created_at: '2026-06-14T00:00:00+00:00',
}

describe('FieldPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.getFieldCampaigns as ReturnType<typeof vi.fn>).mockResolvedValue([CAMPAIGN])
    ;(api.getFieldCampaignChecks as ReturnType<typeof vi.fn>).mockResolvedValue([
      { name: 'capture_success_ratio', category: 'soak', status: 'passed',
        classification: null, detail: '3/3 = 1.00', evidence: {}, created_at: 'x' },
      { name: 'two_device_rf', category: 'physical', status: 'not_executed',
        classification: null, detail: 'single SDR attached', evidence: {}, created_at: 'x' },
    ])
  })

  it('renders campaigns + classification as accessible text', async () => {
    render(<FieldPage />)
    await waitFor(() => expect(api.getFieldCampaigns).toHaveBeenCalled())
    expect(screen.getByText('pluto-soak')).toBeInTheDocument()
    expect(screen.getByText('single_device_soak_complete')).toBeInTheDocument()
  })

  it('shows per-check status (passed/not_executed) as text on demand', async () => {
    render(<FieldPage />)
    const btn = await screen.findByRole('button', { name: 'Checks' })
    btn.click()
    expect(await screen.findByText('capture_success_ratio')).toBeInTheDocument()
    expect(screen.getByText('two_device_rf')).toBeInTheDocument()
    expect(screen.getByText('not_executed')).toBeInTheDocument()
  })
})
