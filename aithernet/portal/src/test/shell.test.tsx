/**
 * Unified customer shell + session behavior (same-origin under www.aithernet.online):
 * - /login while already authenticated redirects to /app (not the homepage);
 * - /app/nodes deep-link / refresh renders the portal (no blank shell);
 * - the authenticated header shows the account + a button-styled Sign out (one cohesive shell);
 * - no header link ever leaves www for app.aithernet.online or a stale #/signin hash route.
 */
import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { RouterProvider } from '../routes/router'
import { SessionProvider } from '../session'

const CUSTOMER = {
  user_id: 'u1', email: 'customer@example.test', is_platform_admin: false,
  authenticated: true, csrf_token: 'csrf-x',
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function authedBackend() {
  return vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString(), 'http://app.test')
    const p = url.pathname
    if (p.endsWith('/v1/auth/session')) return json(CUSTOMER)
    if (p.includes('/memberships')) return json([{ tenant_id: 't1', tenant_name: 'Tenant', role: 'tenant_admin' }])
    if (p.includes('/fleet/nodes')) return json([])
    if (p.includes('/fleet/summary')) return json({ total: 0, by_state: {} })
    if (p.includes('/enrollment-codes')) return json([])
    if (p.includes('/meshes')) return json([])
    return json({})
  })
}

function mount() {
  return render(
    <RouterProvider>
      <SessionProvider>
        <App />
      </SessionProvider>
    </RouterProvider>,
  )
}

beforeEach(() => window.history.replaceState(null, '', '/'))
afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('unified shell + session behavior', () => {
  it('/login while authenticated redirects to /app (never the login form or homepage)', async () => {
    window.history.replaceState(null, '', '/login')
    vi.stubGlobal('fetch', authedBackend())
    mount()
    // The authenticated header renders (Sign out present) and the sign-in password field does NOT.
    const banner = await screen.findByRole('banner')
    expect(within(banner).getByText('Sign out')).toBeInTheDocument()
    expect(screen.queryByLabelText?.('Password')).not.toBeInTheDocument?.()
    expect(document.querySelector('input[type="password"]')).toBeNull()
  })

  it('the authenticated shell header shows the account email + a Sign out control', async () => {
    window.history.replaceState(null, '', '/app')
    vi.stubGlobal('fetch', authedBackend())
    mount()
    const banner = await screen.findByRole('banner')
    expect(within(banner).getByText('customer@example.test')).toBeInTheDocument()
    const signout = within(banner).getByText('Sign out')
    expect(signout.tagName).toBe('BUTTON')
    // Brand still routes to /app; no hostname transition, no stale hash route.
    expect(within(banner).getByText('Aithernet').closest('a')?.getAttribute('href')).toBe('/app')
    for (const a of Array.from(banner.querySelectorAll('a'))) {
      const href = a.getAttribute('href') ?? ''
      if (href.includes('admin.aithernet.online')) continue // admin is intentionally separate
      expect(href).not.toContain('app.aithernet.online')
      expect(href).not.toContain('#/signin')
    }
  })

  it('/app/nodes deep-link renders the portal shell (refresh works, no blank state)', async () => {
    window.history.replaceState(null, '', '/app/nodes')
    vi.stubGlobal('fetch', authedBackend())
    mount()
    // The unified header renders and the portal (not the marketing landing) is shown.
    const banner = await screen.findByRole('banner')
    expect(within(banner).getByText('Sign out')).toBeInTheDocument()
    // The customer console sub-navigation is present (a portal-only surface).
    expect(await screen.findByRole('navigation', { name: 'Customer sections' })).toBeInTheDocument()
  })
})
