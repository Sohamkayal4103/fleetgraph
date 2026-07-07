import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RouterProvider } from '../routes/router'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.location.hash = ''
})

describe('AcceptInvite — existing account requires the existing password', () => {
  it('prompts for the existing password (no create/confirm) when account_exists is true', async () => {
    const validateInvitation = vi.fn().mockResolvedValue({
      valid: true,
      status: 'valid',
      email: 'existing@local.invalid',
      proposed_tenant_name: 'Acme',
      role: 'tenant_admin',
      expires_at: '2026-12-31T00:00:00',
      required_policies: [],
      account_exists: true,
    })
    vi.doMock('../api/client', () => ({
      api: { validateInvitation, acceptInvitation: vi.fn() },
    }))
    vi.doMock('../session', () => ({ useSession: () => ({ signIn: vi.fn() }) }))
    const { AcceptInvite } = await import('../pages/Auth')
    window.location.hash = '#/accept-invite?token=EXIST'
    render(
      <RouterProvider>
        <AcceptInvite />
      </RouterProvider>,
    )
    await screen.findByText('existing@local.invalid')
    // Existing-account UI: prompt for the EXISTING password, explain the account exists,
    // and do NOT offer to create/confirm a new password.
    expect(screen.getByText('Your existing password')).toBeTruthy()
    expect(screen.getByText(/already has an Aithernet account/i)).toBeTruthy()
    expect(screen.queryByText('Create a password')).toBeNull()
    expect(screen.queryByText('Confirm password')).toBeNull()
    vi.doUnmock('../api/client')
    vi.doUnmock('../session')
  })
})
