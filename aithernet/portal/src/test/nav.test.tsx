import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { RouterProvider } from '../routes/router'
import { SessionProvider } from '../session'

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderUnauthenticatedApp() {
  // Every API call (including the session probe) returns 401 -> the provider treats the user as
  // unauthenticated and the unauthenticated header renders.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(JSON.stringify({ detail: 'not_authenticated' }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      }),
    ),
  )
  return render(
    <RouterProvider>
      <SessionProvider>
        <App />
      </SessionProvider>
    </RouterProvider>,
  )
}

describe('portal header is same-origin under www (no hostname transitions)', () => {
  it('the brand navigates to the portal overview (/app), never another hostname', async () => {
    renderUnauthenticatedApp()
    const banner = await screen.findByRole('banner')
    const brand = within(banner).getByText('Aithernet').closest('a')
    expect(brand?.getAttribute('href')).toBe('/app')
    expect(brand?.getAttribute('href')).not.toContain('aithernet.online')
  })

  it('primary-nav public links are same-origin relative paths (no www./app. host, no new tab)', async () => {
    renderUnauthenticatedApp()
    const nav = within(await screen.findByRole('banner')).getByRole('navigation', { name: 'Primary' })
    const expected: Record<string, string> = { Product: '/product', Documentation: '/docs' }
    for (const [label, href] of Object.entries(expected)) {
      const a = within(nav).getByText(label).closest('a')!
      expect(a.getAttribute('href')).toBe(href)
      expect(a.getAttribute('href')).not.toContain('aithernet.online')
    }
  })

  it('Status is a same-origin in-app route', async () => {
    renderUnauthenticatedApp()
    const nav = within(await screen.findByRole('banner')).getByRole('navigation', { name: 'Primary' })
    const status = within(nav).getByText('Status').closest('a')!
    expect(status.getAttribute('href')).toBe('/status')
    expect(status.getAttribute('target')).toBeNull()
  })

  it('Sign in points at /login', async () => {
    renderUnauthenticatedApp()
    const banner = await screen.findByRole('banner')
    const signin = within(banner).getByText('Sign in').closest('a')!
    expect(signin.getAttribute('href')).toBe('/login')
  })

  it('no customer-facing header link targets app.aithernet.online', async () => {
    renderUnauthenticatedApp()
    const banner = await screen.findByRole('banner')
    for (const a of Array.from(banner.querySelectorAll('a'))) {
      expect(a.getAttribute('href') ?? '').not.toContain('app.aithernet.online')
    }
  })

  it('the nav no longer links to the deprecated /platform page (consolidated into /product)', async () => {
    renderUnauthenticatedApp()
    const nav = within(await screen.findByRole('banner')).getByRole('navigation', { name: 'Primary' })
    // Platform is gone as a nav item; Product/Documentation/Status remain.
    expect(within(nav).queryByText('Platform')).toBeNull()
    expect(within(nav).getByText('Product').closest('a')?.getAttribute('href')).toBe('/product')
    for (const a of Array.from(nav.querySelectorAll('a'))) {
      expect(a.getAttribute('href')).not.toBe('/platform')
    }
  })
})
