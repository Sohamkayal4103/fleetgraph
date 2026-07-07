import { describe, expect, it } from 'vitest'

import { safeReturnTo } from '../pages/Auth'

// Post-login return_to validation. Everything is same-origin under www now, so the normal case is a
// relative path; the ONLY approved absolute origin is https://www.aithernet.online. This is the
// open-redirect security gate.
describe('safeReturnTo validates post-login redirect targets (no open redirect)', () => {
  it('accepts same-origin relative paths', () => {
    for (const p of ['/app', '/app/nodes', '/app/accept-invite?token=x', '/status', '/login']) {
      expect(safeReturnTo(p)).toBe(p)
    }
  })

  it('accepts the single approved canonical origin (absolute)', () => {
    for (const url of [
      'https://www.aithernet.online/docs.html',
      'https://www.aithernet.online/app/nodes',
    ]) {
      expect(safeReturnTo(url)).toBe(url)
    }
  })

  it('rejects other Aithernet origins now that everything is under www', () => {
    expect(safeReturnTo('https://aithernet.online/')).toBeNull() // apex
    expect(safeReturnTo('https://app.aithernet.online/#/portal/nodes')).toBeNull() // legacy app host
    expect(safeReturnTo('https://admin.aithernet.online/admin')).toBeNull() // admin origin
  })

  it('rejects external origins and look-alike domains', () => {
    for (const url of [
      'https://evil.example/phish',
      'https://aithernet.online.evil.com/',
      'https://notaithernet.online/',
      'https://www.aithernet.online.evil.com/',
    ]) {
      expect(safeReturnTo(url)).toBeNull()
    }
  })

  it('rejects protocol-relative, backslash and encoded-slash open-redirect variants', () => {
    for (const url of [
      '//evil.example',
      '/\\evil.example',
      '\\\\evil.example',
      '/%2f%2fevil.example',
      '/%5cevil.example',
    ]) {
      expect(safeReturnTo(url)).toBeNull()
    }
  })

  it('rejects userinfo tricks, scheme abuse and HTTP downgrade', () => {
    for (const url of [
      'https://www.aithernet.online@evil.com/',
      'https://evil.com\\@www.aithernet.online/',
      'http://www.aithernet.online/docs.html', // not https
      'javascript:alert(1)',
      'data:text/html,x',
      '   ',
    ]) {
      expect(safeReturnTo(url)).toBeNull()
    }
  })

  it('treats empty / nullish as no redirect', () => {
    expect(safeReturnTo(null)).toBeNull()
    expect(safeReturnTo(undefined)).toBeNull()
    expect(safeReturnTo('')).toBeNull()
  })
})
