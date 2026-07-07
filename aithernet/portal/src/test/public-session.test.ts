/**
 * Behavioral tests for the PUBLIC SITE account-aware header script (aithernet-site session.js),
 * run in the portal's jsdom environment. Verifies: authenticated -> "Open portal"; anonymous ->
 * "Sign in"; unavailable API -> graceful "Sign in" fallback; the pending class is always cleared
 * (no permanent placeholder / no flash trap).
 */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

// The site repo is a sibling of this repo on disk.
const SCRIPT = resolve(__dirname, '../../../../aithernet-site/public/assets/session.js')

function setupDom() {
  document.documentElement.className = 'acct-pending'
  document.body.innerHTML =
    '<a class="btn ghost sm acct-action" id="acct-action" href="/login">Sign in</a>'
  return document.getElementById('acct-action')!
}

function runScript() {
  // Execute the IIFE in the current jsdom realm.
  // eslint-disable-next-line no-new-func
  new Function(readFileSync(SCRIPT, 'utf8'))()
}

async function flush() {
  // Let the fetch promise chain settle.
  await Promise.resolve()
  await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.documentElement.className = ''
})

describe('public-site account-aware header', () => {
  it('the site ships a session.js script', () => {
    expect(existsSync(SCRIPT)).toBe(true)
  })

  it('shows "Open portal" for an authenticated session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ authenticated: true, account: { email: 'op@aithernet.online' } }),
        { status: 200, headers: { 'content-type': 'application/json' } })))
    const el = setupDom()
    runScript()
    await flush()
    expect(el.textContent).toBe('Open portal')
    expect(el.getAttribute('href')).toBe('/app')
    expect(document.documentElement.classList.contains('acct-pending')).toBe(false)
  })

  it('shows "Sign in" (with return_to) for an anonymous session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ authenticated: false }),
        { status: 200, headers: { 'content-type': 'application/json' } })))
    const el = setupDom()
    runScript()
    await flush()
    expect(el.textContent).toBe('Sign in')
    expect(el.getAttribute('href')).toContain('/login?return_to=')
    expect(el.getAttribute('href')).not.toContain('aithernet.online')
    expect(document.documentElement.classList.contains('acct-pending')).toBe(false)
  })

  it('falls back to "Sign in" and reveals when the status check fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down') }))
    const el = setupDom()
    runScript()
    await flush()
    expect(el.textContent).toBe('Sign in')
    expect(document.documentElement.classList.contains('acct-pending')).toBe(false)
  })

  it('requests the status endpoint with credentials included', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ authenticated: false }),
        { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    setupDom()
    runScript()
    await flush()
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/auth/session-status')
    expect(opts.credentials).toBe('include')
  })
})
