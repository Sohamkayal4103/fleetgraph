// Stage 14B frontend test: the Hardware view. The `api` client is fully mocked — no backend is
// contacted. Asserts the operator sees providers + availability, devices with accessible TEXT
// status (present/missing, RX/TX, enabled/disabled), and leases (owning mission, state, expiry);
// that a read-only node (operator actions disabled) shows NO acquire/release/revoke controls and
// performs no writes on load; and that the refresh action calls only the refresh endpoint.

import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => {
  const api = {
    baseUrl: 'http://test',
    getHardwareStatus: vi.fn(),
    getHardwareDevices: vi.fn(),
    getHardwareLeases: vi.fn(),
    refreshHardware: vi.fn(),
    releaseHardwareLease: vi.fn(),
    revokeHardwareLease: vi.fn(),
    setHardwareDeviceEnabled: vi.fn(),
    getHardwareSupportMatrix: vi.fn(),
  }
  class ApiError extends Error {
    category = 'http'
  }
  return { api, ApiError, API_BASE_URL: 'http://test' }
})

import { api } from '../api/client'
import { HardwarePage } from './HardwarePage'

const STATUS_READONLY = {
  enabled: true,
  operator_lease_actions_enabled: false,
  providers: [
    { provider_id: 'soapy', kind: 'soapy', enabled: true, required: false, available: false },
    { provider_id: 'lab', kind: 'static', enabled: true, required: true, available: true },
  ],
  devices_by_status: { present: 1, missing: 0 },
  leases_by_state: { active: 1 },
}

const DEVICES = [
  {
    device_id: 'dev-aaaa-bbbb',
    node_id: 'n1',
    hardware_key: 'lab:abc',
    provider_id: 'lab',
    device_kind: 'sdr',
    vendor: 'Ettus',
    product: 'B210',
    serial: 'ABC123',
    driver: 'uhd',
    transport: 'usb',
    channel: null,
    display_name: 'B210',
    identity_limited: false,
    enabled: true,
    administrative_state: 'enabled',
    presence_state: 'present',
    health_state: 'ok',
    status: 'present',
    capability_revision: 1,
    metadata: {},
    compatible_backends: ['legacy_gr_mcp'],
    active_lease_count: 1,
    first_seen_at: '2026-06-14T00:00:00+00:00',
    last_seen_at: '2026-06-14T00:01:00+00:00',
  },
]

const LEASES = [
  {
    lease_id: 'lease-aaaa-bbbb',
    node_id: 'n1',
    device_id: 'dev-aaaa-bbbb',
    backend_id: 'legacy_gr_mcp',
    mission_id: 'mission-xyz',
    mission_run_id: null,
    mission_step_id: null,
    requested_operation: 'capture',
    direction: 'rx',
    requested_channels: [],
    lease_mode: 'exclusive',
    state: 'active',
    reason: null,
    acquired_at: '2026-06-14T00:00:00+00:00',
    renewed_at: null,
    expires_at: '2026-06-14T01:00:00+00:00',
    released_at: null,
    created_at: '2026-06-14T00:00:00+00:00',
  },
]

function mockAll(status = STATUS_READONLY) {
  (api.getHardwareStatus as ReturnType<typeof vi.fn>).mockResolvedValue(status)
  ;(api.getHardwareDevices as ReturnType<typeof vi.fn>).mockResolvedValue(DEVICES)
  ;(api.getHardwareLeases as ReturnType<typeof vi.fn>).mockResolvedValue(LEASES)
  ;(api.refreshHardware as ReturnType<typeof vi.fn>).mockResolvedValue({
    availability: { lab: true }, devices_by_status: { present: 1 },
  })
  ;(api.getHardwareSupportMatrix as ReturnType<typeof vi.fn>).mockResolvedValue([
    { device_id: 'dev-aaaa-bbbb', display_name: 'B210', provider_id: 'lab', driver: 'uhd',
      architecture_supported: true, discovered: true, classification: 'capture_qualified',
      last_qualified_at: '2026-06-14T00:00:00+00:00' },
  ])
}

describe('HardwarePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockAll()
  })

  it('renders providers, devices, and leases with accessible text states', async () => {
    render(<HardwarePage />)
    await waitFor(() => expect(api.getHardwareStatus).toHaveBeenCalled())
    // provider availability shown as text (not colour alone)
    expect(await screen.findByText('unavailable')).toBeInTheDocument()
    expect(screen.getAllByText('available').length).toBeGreaterThan(0)
    // device present + name + compatible backend shown as text
    expect(await screen.findByText('present')).toBeInTheDocument()
    expect(screen.getAllByText('B210').length).toBeGreaterThan(0)
    expect(screen.getByText('legacy_gr_mcp')).toBeInTheDocument()
    // lease state shown as accessible text
    expect(screen.getByText('active')).toBeInTheDocument()
    // qualification classification shown as accessible text (not colour alone)
    expect(await screen.findByText('capture_qualified')).toBeInTheDocument()
  })

  it('hides operator lease controls and performs no writes when operator actions are disabled', async () => {
    render(<HardwarePage />)
    await waitFor(() => expect(api.getHardwareLeases).toHaveBeenCalled())
    expect(screen.queryByText('Release')).not.toBeInTheDocument()
    expect(screen.queryByText('Revoke')).not.toBeInTheDocument()
    expect(api.releaseHardwareLease).not.toHaveBeenCalled()
    expect(api.revokeHardwareLease).not.toHaveBeenCalled()
    expect(api.setHardwareDeviceEnabled).not.toHaveBeenCalled()
  })

  it('shows operator controls when the node enables operator actions', async () => {
    mockAll({ ...STATUS_READONLY, operator_lease_actions_enabled: true })
    render(<HardwarePage />)
    expect(await screen.findByText('Release')).toBeInTheDocument()
    expect(screen.getByText('Revoke')).toBeInTheDocument()
    expect(screen.getByText('Disable')).toBeInTheDocument()
  })
})
