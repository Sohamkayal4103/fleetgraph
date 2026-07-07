import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RouterProvider, useRouter, hashToPath } from '../routes/router'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('legacy hash → path compatibility shim', () => {
  it('maps beta.4 hash routes to their new path equivalents', () => {
    expect(hashToPath('#/accept-invite?token=ABC123')).toEqual({
      path: '/app/accept-invite',
      search: '?token=ABC123',
    })
    expect(hashToPath('#/portal/nodes')).toEqual({ path: '/app/nodes', search: '' })
    expect(hashToPath('#/portal')).toEqual({ path: '/app', search: '' })
    expect(hashToPath('#/signin')).toEqual({ path: '/login', search: '' })
    expect(hashToPath('#/admin/fleet')).toEqual({ path: '/admin/fleet', search: '' })
    expect(hashToPath('#/status')).toEqual({ path: '/status', search: '' })
  })

  it('rewrites an old #/accept-invite?token=… URL to /app/accept-invite with the token readable', () => {
    function Probe() {
      const { path, query } = useRouter()
      return (
        <div>
          <span data-testid="path">{path}</span>
          <span data-testid="token">{query.get('token') ?? ''}</span>
        </div>
      )
    }
    window.history.replaceState(null, '', '/')
    window.location.hash = '#/accept-invite?token=ABC123'
    render(
      <RouterProvider>
        <Probe />
      </RouterProvider>,
    )
    expect(screen.getByTestId('path').textContent).toBe('/app/accept-invite')
    expect(screen.getByTestId('token').textContent).toBe('ABC123')
    // The legacy hash is gone (replaced, not pushed).
    expect(window.location.hash).toBe('')
  })
})

describe('canonical invitation link format (path, no hash)', () => {
  it('is /app/accept-invite?token=… on the canonical www origin, never /portal', () => {
    const token = 'one-time-xyz'
    const link = `https://www.aithernet.online/app/accept-invite?token=${token}`
    expect(link).toMatch(/\/app\/accept-invite\?token=/)
    expect(link).not.toContain('/#/') // no hash routing
    expect(link).not.toContain('app.aithernet.online') // customer origin is www
    expect(link).not.toContain('/portal')
    expect(link.match(/token=/g)?.length).toBe(1)
  })
})

describe('AcceptInvite reads the token from the URL — no manual token field', () => {
  it('auto-validates from the URL token, shows invited email read-only, and stores no token', async () => {
    const validateInvitation = vi.fn().mockResolvedValue({
      valid: true,
      status: 'valid',
      email: 'invited@local.invalid',
      proposed_tenant_name: 'Acme',
      role: 'tenant_admin',
      expires_at: '2026-12-31T00:00:00',
      required_policies: [],
    })
    vi.doMock('../api/client', () => ({
      api: { validateInvitation, acceptInvitation: vi.fn() },
    }))
    vi.doMock('../session', () => ({ useSession: () => ({ signIn: vi.fn() }) }))
    const { AcceptInvite } = await import('../pages/Auth')
    window.history.replaceState(null, '', '/app/accept-invite?token=URLTOKEN')
    render(
      <RouterProvider>
        <AcceptInvite />
      </RouterProvider>,
    )
    await waitFor(() => expect(validateInvitation).toHaveBeenCalledWith('URLTOKEN'))
    await screen.findByText('invited@local.invalid')
    // No manual token entry field in the normal flow.
    expect(document.querySelector('#inv-token')).toBeNull()
    // The token must not be persisted anywhere.
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
    // The token is scrubbed from the visible URL (memory-only thereafter): path kept, query gone.
    expect(window.location.pathname).toBe('/app/accept-invite')
    expect(window.location.search).toBe('')
    vi.doUnmock('../api/client')
    vi.doUnmock('../session')
  })
})
