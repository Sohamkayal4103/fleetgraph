// Migration-specific behavior for the unified www origin: path-routed /app deep links, direct
// refresh, the anonymous -> /login gate, an already-authenticated visit to /login, and /logout.
// Driven through the REAL App/router with a mocked control-plane boundary (the only network edge).
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { RouterProvider } from '../routes/router'
import { SessionProvider } from '../session'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}
type Handler = (url: URL, init: RequestInit) => unknown
function backend(routes: Array<[RegExp, string, Handler]>) {
  return vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString(), 'http://www.test')
    const method = (init.method ?? 'GET').toUpperCase()
    for (const [re, m, h] of routes) if (m === method && re.test(url.pathname)) return json(h(url, init))
    return json({ detail: 'not_found' }, 404)
  })
}

const AUTHED = { user_id: 'u1', email: 'c@example.test', is_platform_admin: false, authenticated: true, csrf_token: 'x' }
const AUTHED_BACKEND: Array<[RegExp, string, Handler]> = [
  [/\/v1\/auth\/session$/, 'GET', () => AUTHED],
  [/\/v1\/memberships/, 'GET', () => [{ tenant_id: 't1', tenant_name: 'Acme', role: 'tenant_admin' }]],
  [/\/v1\/auth\/logout/, 'POST', () => ({ logged_out: true })],
  [/.*/, 'GET', () => []],
]

function mount() {
  return render(
    <RouterProvider><SessionProvider><App /></SessionProvider></RouterProvider>,
  )
}

beforeEach(() => window.history.replaceState(null, '', '/'))
afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('unified www origin — path-routed portal', () => {
  it('/app renders the customer overview', async () => {
    window.history.replaceState(null, '', '/app')
    vi.stubGlobal('fetch', backend(AUTHED_BACKEND))
    mount()
    expect(await screen.findByText('Customer overview')).toBeInTheDocument()
  })

  it('a direct /app/nodes deep link (a refresh) renders the Nodes section, not the overview', async () => {
    window.history.replaceState(null, '', '/app/nodes')
    vi.stubGlobal('fetch', backend(AUTHED_BACKEND))
    mount()
    expect(await screen.findByText('Enroll a node')).toBeInTheDocument()
  })

  it('a direct /app/meshes deep link renders the Meshes section', async () => {
    window.history.replaceState(null, '', '/app/meshes')
    vi.stubGlobal('fetch', backend(AUTHED_BACKEND))
    mount()
    expect(await screen.findByText('Create a mesh')).toBeInTheDocument()
  })

  it('an anonymous visit to /app shows a same-origin sign-in gate pointing at /login', async () => {
    window.history.replaceState(null, '', '/app')
    // Every API call (incl. the session probe) 401s -> the portal renders its sign-in gate.
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'not_authenticated' }, 401)))
    mount()
    expect(await screen.findByText('Sign in to your account')).toBeInTheDocument()
    const signin = screen.getAllByText('Sign in').map((n) => n.closest('a')).find(Boolean)
    expect(signin?.getAttribute('href')).toBe('/login')
  })

  it('an already-authenticated visit to /login lands on /app (no second login form)', async () => {
    window.history.replaceState(null, '', '/login')
    vi.stubGlobal('fetch', backend(AUTHED_BACKEND))
    mount()
    await waitFor(() => expect(window.location.pathname).toBe('/app'))
    expect(await screen.findByText('Customer overview')).toBeInTheDocument()
  })

  it('/logout revokes the session server-side and lands on /login', async () => {
    window.history.replaceState(null, '', '/logout')
    // One fetch mock: anonymous session probe (401, so there is no probe/logout ordering race),
    // and a 200 for the logout POST. It records every call so we can assert the POST happened.
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://www.test').pathname
      if (path.endsWith('/v1/auth/logout')) return json({ logged_out: true })
      return json({ detail: 'not_authenticated' }, 401)
    })
    vi.stubGlobal('fetch', fetchMock)
    mount()
    await waitFor(() => expect(window.location.pathname).toBe('/login'))
    const calledLogout = fetchMock.mock.calls.some(
      ([u, i]) => String(u).includes('/v1/auth/logout') && ((i as RequestInit)?.method ?? '').toUpperCase() === 'POST',
    )
    expect(calledLogout).toBe(true)
  })
})
