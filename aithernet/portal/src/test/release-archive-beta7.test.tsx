// beta.7 (FIX 12) — the customer downloads view groups releases as current / previous / archive,
// newest-first regardless of API order, and keeps older releases available in the archive.
import { render, screen, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { RouterProvider } from '../routes/router'
import { SessionProvider } from '../session'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}
function backend(routes: Array<[RegExp, string, (u: URL) => unknown]>) {
  return vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString(), 'http://app.test')
    const method = (init.method ?? 'GET').toUpperCase()
    for (const [re, m, handler] of routes) if (m === method && re.test(url.pathname)) return json(handler(url))
    return json({ detail: 'not_found' }, 404)
  })
}
const AUTHED = { user_id: 'u1', email: 'c@test', is_platform_admin: false, authenticated: true, csrf_token: 'x' }
function rel(version: string, id: string) {
  return {
    release_id: id, version, channel: 'early-access', status: 'published',
    signing_key_id: 'k', published_at: '2026-07-01T00:00:00Z',
    artifacts: [{ name: `aithernet_${version}_amd64.deb`, kind: 'deb', architecture: 'amd64',
      os_family: 'ubuntu-24.04', sha256: 'sha256:1111', byte_size: 4096, content_type: 'x' }],
  }
}
function mount() {
  return render(<RouterProvider><SessionProvider><App /></SessionProvider></RouterProvider>)
}

beforeEach(() => window.history.replaceState(null, '', '/app/downloads'))
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/') })

describe('release archive grouping (current / previous / archive)', () => {
  it('marks the newest as current, the next as previous, and archives the rest', async () => {
    // Deliberately out of order to prove the client sorts by version, not API order.
    const releases = [rel('1.0.0-beta.5', 'r5'), rel('1.0.0-beta.7', 'r7'),
      rel('1.0.0-beta.4', 'r4'), rel('1.0.0-beta.6', 'r6')]
    vi.stubGlobal('fetch', backend([
      [/\/v1\/auth\/session/, 'GET', () => AUTHED],
      [/\/v1\/memberships/, 'GET', () => [{ tenant_id: 't1', tenant_name: 'A', role: 'tenant_admin' }]],
      [/\/v1\/portal\/releases\/.+\/bundle$/, 'GET', () => ({
        filename: 'b.zip', sha256: 'sha256:aaa', byte_size: 1000, files: [] })],
      [/\/v1\/releases/, 'GET', () => releases],
      [/.*/, 'GET', () => []],
    ]))
    mount()
    // current = beta.7 (recommended)
    expect(await screen.findByText(/Aithernet 1\.0\.0-beta\.7 — recommended for Ubuntu 24\.04/))
      .toBeInTheDocument()
    // previous = beta.6
    expect(await screen.findByText(/Aithernet 1\.0\.0-beta\.6 — previous release/)).toBeInTheDocument()
    // beta.4 / beta.5 are inside the Archive disclosure
    const archive = await screen.findByText(/Archive — older releases \(2\)/)
    expect(archive).toBeInTheDocument()
    fireEvent.click(archive)
    expect(await screen.findByText(/Aithernet 1\.0\.0-beta\.5$/)).toBeInTheDocument()
    expect(await screen.findByText(/Aithernet 1\.0\.0-beta\.4$/)).toBeInTheDocument()
    // "current" and "previous" badges are present exactly once each
    expect(screen.getByText('current')).toBeInTheDocument()
    expect(screen.getByText('previous')).toBeInTheDocument()
  })
})

describe('client documentation section', () => {
  it('renders the beta.7 client guide with CatGPT/noVNC/sandbox sections', async () => {
    window.history.replaceState(null, '', '/app/docs')
    vi.stubGlobal('fetch', backend([
      [/\/v1\/auth\/session/, 'GET', () => AUTHED],
      [/\/v1\/memberships/, 'GET', () => [{ tenant_id: 't1', tenant_name: 'A', role: 'tenant_admin' }]],
      [/\/v1\/releases/, 'GET', () => [rel('1.0.0-beta.7', 'r7')]],
      [/.*/, 'GET', () => []],
    ]))
    mount()
    expect(await screen.findByText(/Managed CatGPT Gateway \(coordinator\)/)).toBeInTheDocument()
    expect(screen.getByText(/Sandbox \/ AppArmor/)).toBeInTheDocument()
    expect(screen.getByText(/Troubleshooting/)).toBeInTheDocument()
  })
})
