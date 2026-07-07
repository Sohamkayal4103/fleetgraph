import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getPolicyStatus } from '../api/client'
import { ErrorBoundary } from '../components/ErrorBoundary'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('getPolicyStatus normalizes the server object into a per-policy array', () => {
  it('maps {active,accepted} into PolicyStatus[] (so the UI .find never throws)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            active: [
              { policy_id: 'p1', policy_type: 'preview_terms', version: 'v1' },
              { policy_id: 'p2', policy_type: 'privacy', version: 'v2' },
            ],
            accepted: [{ policy_type: 'preview_terms', version: 'v1' }],
            outstanding: [],
            onboarding_complete: false,
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      ),
    )
    const status = await getPolicyStatus()
    expect(Array.isArray(status)).toBe(true)
    expect(typeof status.find).toBe('function') // regression: was an object -> .find crashed
    expect(status.find((s) => s.policy_id === 'p1')?.accepted).toBe(true)
    expect(status.find((s) => s.policy_id === 'p2')?.accepted).toBe(false)
  })
})

describe('ErrorBoundary confines a render error', () => {
  function Boom(): never {
    throw new Error('boom')
  }
  it('renders a fallback instead of unmounting the whole tree', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/could not be displayed/i)).toBeInTheDocument()
  })
})
