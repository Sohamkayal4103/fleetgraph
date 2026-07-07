// Browser-level (SPA) end-to-end coverage of the customer journey, driven through the REAL portal
// components with a stateful in-memory mock of the control-plane HTTP boundary (the only network
// edge). These exercise the same code paths a real browser runs: session probe, membership gate,
// release listing, the recommended-bundle download, the advanced artifact view, the policy gate,
// the not-provisioned state, marketing-route redirects and the public status page.
//
// Cross-domain navigation (www <-> app), real SMTP delivery and real DB records cannot be expressed
// in jsdom; those legs of the 24-case matrix are covered by the backend pytest suite
// (tests/test_intake.py, test_release_download.py, ...) and by the live post-deploy verification.
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { RouterProvider } from '../routes/router'
import { SessionProvider } from '../session'

// --- a tiny stateful mock backend -------------------------------------------------------------
type Handler = (url: URL, init: RequestInit) => unknown | { __status: number; body: unknown }
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}
function backend(routes: Array<[RegExp, string, Handler]>) {
  return vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString(), 'http://app.test')
    const method = (init.method ?? 'GET').toUpperCase()
    for (const [re, m, handler] of routes) {
      if (m === method && re.test(url.pathname)) {
        const out = handler(url, init)
        if (out && typeof out === 'object' && '__status' in out) {
          const o = out as unknown as { __status: number; body: unknown }
          return json(o.body, o.__status)
        }
        return json(out)
      }
    }
    return json({ detail: 'not_found' }, 404)
  })
}

const AUTHED = {
  user_id: 'u-cust-1',
  email: 'customer@example.test',
  is_platform_admin: false,
  authenticated: true,
  csrf_token: 'csrf-x',
}
const BETA5 = {
  release_id: 'rel-beta5',
  version: '0.8.0-beta.5',
  channel: 'early-access',
  status: 'published',
  manifest_digest: 'sha256:deadbeef',
  signing_key_id: 'aithernet-release-2025',
  published_at: '2026-06-01T00:00:00Z',
  artifacts: [
    { name: 'aithernet_0.8.0-beta.5_amd64.deb', kind: 'deb', architecture: 'amd64', os_family: 'ubuntu-24.04', sha256: 'sha256:1111', byte_size: 4096, content_type: 'application/vnd.debian.binary-package' },
    { name: 'aithernet-0.8.0b5-py3-none-any.whl', kind: 'wheel', architecture: 'any', os_family: 'any', sha256: 'sha256:2222', byte_size: 2048, content_type: 'application/zip' },
    { name: 'rf-mcp-0.1.0+aithernet.2-src.tar.gz', kind: 'component-source', architecture: 'any', os_family: 'any', sha256: 'sha256:3333', byte_size: 8192, content_type: 'application/gzip' },
    { name: 'rf-mcp-0.1.0+aithernet.2-src.tar.gz.sig', kind: 'signature', architecture: 'any', os_family: 'any', sha256: 'sha256:4444', byte_size: 64, content_type: 'application/octet-stream' },
    { name: 'aithernet-component.pub', kind: 'pubkey', architecture: 'any', os_family: 'any', sha256: 'sha256:5555', byte_size: 128, content_type: 'application/x-pem-file' },
    { name: 'lock.json', kind: 'metadata', architecture: 'any', os_family: 'any', sha256: 'sha256:6666', byte_size: 256, content_type: 'application/json' },
  ],
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

beforeEach(() => {
  window.history.replaceState(null, '', '/')
})
afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('public surfaces are reachable and not dead ends (same-origin under www)', () => {
  it('the unauthenticated shell header offers home + sign in + public nav — all same-origin', async () => {
    vi.stubGlobal('fetch', backend([[/.*/, 'GET', () => ({ __status: 401, body: { detail: 'x' } })]]))
    mount()
    const banner = await screen.findByRole('banner')
    const b = within(banner)
    expect(b.getByText('Home').closest('a')?.getAttribute('href')).toBe('/')
    // Sign in is the button-styled account action -> /login (same origin).
    expect(b.getByText('Sign in').closest('a')?.getAttribute('href')).toBe('/login')
    // The unified public nav uses clean same-origin paths.
    expect(b.getByText('Product').closest('a')?.getAttribute('href')).toBe('/product')
    expect(b.getByText('Documentation').closest('a')?.getAttribute('href')).toBe('/docs')
    // No customer-facing header link leaves www for app.aithernet.online or a stale hash route.
    for (const a of Array.from(banner.querySelectorAll('a'))) {
      const href = a.getAttribute('href') ?? ''
      expect(href).not.toContain('app.aithernet.online')
      expect(href).not.toContain('#/signin')
    }
    // The footer matches the public style with same-origin relative links.
    const footer = screen.getByRole('contentinfo')
    expect(within(footer).getByText('Product').closest('a')?.getAttribute('href')).toBe('/product')
    expect(within(footer).getByText('Privacy & legal').closest('a')?.getAttribute('href')).toBe('/privacy')
    expect(footer.textContent).not.toMatch(/Control plane|0\.8\.0-beta\.1/)
  })

  it('the public /status route renders the live status page in-app (no hostname transition)', async () => {
    window.history.replaceState(null, '', '/status')
    vi.stubGlobal('fetch', backend([
      [/\/health\/ready/, 'GET', () => ({ status: 'ready' })],
      [/\/health\/diagnostics/, 'GET', () => ({ counts: { published_releases: 1 } })],
      [/.*/, 'GET', () => ({ __status: 401, body: {} })],
    ]))
    mount()
    expect(await screen.findByText('Service status')).toBeInTheDocument()
  })
})

describe('signed-in but not yet provisioned', () => {
  it('shows a distinct "access is being set up" state, not a sign-in loop', async () => {
    window.history.replaceState(null, '', '/app')
    vi.stubGlobal('fetch', backend([
      [/\/v1\/auth\/session/, 'GET', () => AUTHED],
      [/\/v1\/memberships/, 'GET', () => []],
      [/.*/, 'GET', () => []],
    ]))
    mount()
    expect(await screen.findByText('Your access is being set up')).toBeInTheDocument()
    expect(screen.getByText('Accept an invitation')).toBeInTheDocument()
    // It must NOT present the "Sign in to your account" gate (would be a loop for a signed-in user).
    expect(screen.queryByText('Sign in to your account')).not.toBeInTheDocument()
  })
})

describe('approved customer — releases & downloads', () => {
  function authedBackend(extra: Array<[RegExp, string, Handler]> = []) {
    return backend([
      ...extra, // overrides win (first match): a test can shadow a base route below
      [/\/v1\/auth\/session/, 'GET', () => AUTHED],
      [/\/v1\/memberships/, 'GET', () => [{ tenant_id: 't-1', tenant_name: 'Acme', role: 'tenant_admin' }]],
      [/\/v1\/portal\/releases\/.+\/bundle$/, 'GET', () => ({
        filename: 'aithernet-0.8.0-beta.5-ubuntu24.04-amd64.zip',
        sha256: 'sha256:abc123def456',
        byte_size: 16_500_000,
        files: [{ name: 'aithernet_0.8.0~beta.5_amd64.deb', sha256: 'sha256:1111' }],
      })],
      [/\/v1\/releases/, 'GET', () => [BETA5]],
      [/.*/, 'GET', () => []],
    ])
  }

  it('shows beta.5 with one ZIP download link, the ZIP SHA-256, and correct target metadata', async () => {
    window.history.replaceState(null, '', '/app/downloads')
    vi.stubGlobal('fetch', authedBackend())
    mount()
    expect(await screen.findByText(/Aithernet 0\.8\.0-beta\.5 — recommended for Ubuntu 24\.04/)).toBeInTheDocument()
    // ONE authenticated ZIP download link (not a multi-file browser batch).
    const zip = await screen.findByText('Download Ubuntu 24.04 amd64 bundle (.zip)')
    expect(zip.closest('a')?.getAttribute('href')).toBe('/api/v1/portal/releases/rel-beta5/bundle.zip')
    // The ZIP SHA-256 (from the bundle meta) is displayed for the customer to confirm.
    expect(await screen.findByText('abc123def456')).toBeInTheDocument()
    expect(screen.getByText(/aithernet-0\.8\.0-beta\.5-ubuntu24\.04-amd64\.zip/)).toBeInTheDocument()
    // Individual files remain under Advanced — all artifacts, with corrected target metadata.
    fireEvent.click(screen.getByText('Advanced — all artifacts'))
    expect(await screen.findByText('Ubuntu 24.04 · amd64')).toBeInTheDocument()
    expect(screen.queryByText('any / any')).not.toBeInTheDocument()
  })

  it('a required-policy 403 shows an accept-the-terms gate, not a download table', async () => {
    window.history.replaceState(null, '', '/app/downloads')
    vi.stubGlobal('fetch', authedBackend([
      [/\/v1\/releases/, 'GET', () => ({ __status: 403, body: { detail: 'required_policies_not_accepted' } })],
    ]))
    mount()
    expect(await screen.findByText(/accept the required terms/)).toBeInTheDocument()
    expect(screen.getByText('Review & accept the required policy').closest('a')?.getAttribute('href')).toBe('/app/policies')
  })
})
