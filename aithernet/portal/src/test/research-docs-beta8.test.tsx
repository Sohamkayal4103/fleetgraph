// The client Documentation section includes the research-recording guide. As of beta.9 this is
// "Research recording & owner-archive upload": automatic recording after consent, upload to the
// Aithernet OWNER archive (not a client Google Drive), the real data commands, and privacy.
import { render, screen } from '@testing-library/react'
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

beforeEach(() => window.history.replaceState(null, '', '/app/docs'))
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/') })

describe('research recording docs (owner-archive upload)', () => {
  it('documents automatic recording, the data commands, and owner-archive upload', async () => {
    vi.stubGlobal('fetch', backend([
      [/\/v1\/auth\/session/, 'GET', () => AUTHED],
      [/\/v1\/memberships/, 'GET', () => [{ tenant_id: 't1', tenant_name: 'A', role: 'tenant_admin' }]],
      [/\/v1\/releases/, 'GET', () => [rel('1.0.0-beta.9', 'r9')]],
      [/.*/, 'GET', () => []],
    ]))
    mount()
    expect(await screen.findByText(/Research recording & owner-archive upload/)).toBeInTheDocument()
    // automatic recording claim
    expect(screen.getByText(/every completed mission/i)).toBeInTheDocument()
    // the real data commands are present in the guide
    const pre = document.body.textContent ?? ''
    expect(pre).toContain('aithernet data status')
    expect(pre).toContain('aithernet data research-consent')
    expect(pre).toContain('aithernet data upload-now')
    // owner-archive story (not a client Google Drive) + privacy
    expect(pre).toContain('owner archive')
    expect(pre).toContain('no Google Drive setup')
    expect(pre).toContain('never uploaded')
  })
})
