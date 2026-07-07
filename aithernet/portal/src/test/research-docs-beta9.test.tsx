// beta.9 — the client Documentation section's research subsection is "Research recording &
// owner-archive upload". Data goes to the Aithernet OWNER archive after consent (not the client's
// own Google Drive); there is no client-side Drive setup; `data drive-client`/`data sync` are only
// the ADVANCED standalone/local-archive path, never the normal one.
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

describe('beta.9 research recording docs (owner-archive upload)', () => {
  it('documents the owner archive, no client Drive setup, and the real data commands', async () => {
    vi.stubGlobal('fetch', backend([
      [/\/v1\/auth\/session/, 'GET', () => AUTHED],
      [/\/v1\/memberships/, 'GET', () => [{ tenant_id: 't1', tenant_name: 'A', role: 'tenant_admin' }]],
      [/\/v1\/releases/, 'GET', () => [rel('1.0.0-beta.9', 'r9')]],
      [/.*/, 'GET', () => []],
    ]))
    mount()
    // New subsection title.
    expect(await screen.findByText(/Research recording & owner-archive upload/)).toBeInTheDocument()
    const body = document.body.textContent ?? ''
    // Data goes to the owner archive, managed by hosted — not to a client Drive.
    expect(body).toContain('owner archive')
    expect(body).toMatch(/managed by hosted|owner archive/)
    expect(body).toContain('no Google Drive setup')
    // The real data commands are present.
    expect(body).toContain('aithernet data research-consent')
    expect(body).toContain('aithernet data upload-now')
    // The setup question now uploads to the owner archive.
    expect(body).toContain('upload to the Aithernet owner archive')
    // What is NEVER collected is spelled out (secret-scanned + quarantined).
    expect(body).toContain('CATGPT_GATEWAY_API_KEY')
    expect(body).toMatch(/hosted-prod\.env/)
    // Opt out via consent withdraw.
    expect(body).toContain('aithernet data research-consent --withdraw')

    // beta.8 client-Drive-DEFAULT wording is gone: no install-your-own OAuth client, no
    // "local-only until you install your own", no "drive-client install" as the normal path.
    expect(body).not.toContain('drive-client install')
    expect(body).not.toContain('local-only until you install your own')
    expect(body).not.toContain('install your own')
    // `data drive-client` / `data sync` survive ONLY under the advanced/standalone label.
    expect(body).toMatch(/Advanced \/ standalone only/)
  })
})
