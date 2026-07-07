// Deterministic edge-routing policy tests. Run: `node --test deploy/edge/`.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { route, isAppPath, normalizePath, marketingRewrite, STRIP_INBOUND_HEADERS } from './routing.mjs'

test('portal SPA + auth + status paths route to the app origin', () => {
  for (const p of [
    '/app', '/app/', '/app/nodes', '/app/meshes', '/app/downloads', '/app/data',
    '/app/account', '/app/support', '/app/getting-started', '/app/policies', '/app/sessions',
    '/app/accept-invite', '/app/assets/index-abc123.js',
    '/login', '/logout', '/status', '/reset', '/accept-invite',
  ]) {
    assert.equal(route(p).origin, 'app', `${p} should be app`)
  }
})

test('same-origin API + node/programmatic paths route to the app origin', () => {
  for (const p of [
    '/api/v1/auth/login', '/api/v1/auth/session', '/api/v1/fleet/nodes', '/api/v1/releases',
    '/ingest/v1/readiness', '/v1/downloads/rel/aithernet.pub', '/v1/public/releases',
    '/v1/node/enroll', '/v1/node/heartbeat', '/health/ready', '/health/diagnostics',
  ]) {
    assert.equal(route(p).origin, 'app', `${p} should be app`)
  }
})

test('marketing paths route to the marketing origin with extensionless -> .html', () => {
  const cases = {
    '/': '/',
    '/product': '/product.html',
    '/platform': '/platform.html',
    '/use-cases': '/use-cases.html',
    '/security': '/security.html',
    '/docs': '/docs.html',
    '/privacy': '/privacy.html',
    '/request-access': '/request-access.html',
    '/api': '/api.html', // bare /api is the marketing API reference page, NOT the control-plane API
    '/assets/style.css': '/assets/style.css', // already extensioned
    '/blog/': '/blog/', // directory: Pages serves index.html
  }
  for (const [inp, out] of Object.entries(cases)) {
    const r = route(inp)
    assert.equal(r.origin, 'marketing', `${inp} should be marketing`)
    assert.equal(r.upstreamPath, out, `${inp} -> ${out}`)
  }
})

test('the API prefix and the marketing /api page do not collide', () => {
  assert.equal(isAppPath('/api/v1/x'), true)
  assert.equal(isAppPath('/api'), false)
  assert.equal(isAppPath('/api.html'), false)
})

test('path-boundary matching: /apparel is NOT the app', () => {
  assert.equal(isAppPath('/apparel'), false)
  assert.equal(isAppPath('/status-page'), false)
  assert.equal(isAppPath('/logout'), true)
})

test('mutating methods on the static marketing origin are a deterministic 405', () => {
  for (const m of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    const r = route('/product', m)
    assert.equal(r.allow, false)
    assert.equal(r.status, 405)
  }
  // ...but mutating methods to the app/API origin are allowed (method preserved for the backend).
  assert.equal(route('/api/v1/auth/login', 'POST').allow, true)
  assert.equal(route('/api/v1/fleet/nodes/x/revoke', 'POST').origin, 'app')
})

test('query strings are never part of the routing decision (caller preserves them)', () => {
  // route() only sees the pathname; callers append the untouched query.
  assert.equal(route('/app/accept-invite').origin, 'app')
  assert.equal(route('/login').origin, 'app')
})

test('dot-segment / double-slash normalization cannot escape the allowlist', () => {
  assert.equal(normalizePath('/app/../admin'), '/admin')
  assert.equal(normalizePath('/app//nodes'), '/app/nodes')
  assert.equal(normalizePath('/./app'), '/app')
  // A crafted "/app/../api/v1" normalizes to /api/v1 and is still (correctly) an app path — it does
  // NOT reach any admin surface (admin is a different browser origin, never routed from www).
  assert.equal(isAppPath(normalizePath('/app/../api/v1/x')), true)
})

test('the strip-list covers every spoofable inbound forwarding header', () => {
  for (const h of ['x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto', 'x-real-ip', 'forwarded']) {
    assert.ok(STRIP_INBOUND_HEADERS.includes(h), `${h} must be stripped`)
  }
})

test('marketingRewrite leaves already-extensioned and directory paths alone', () => {
  assert.equal(marketingRewrite('/favicon.svg'), '/favicon.svg')
  assert.equal(marketingRewrite('/sitemap.xml'), '/sitemap.xml')
  assert.equal(marketingRewrite('/assets/session.js'), '/assets/session.js')
  assert.equal(marketingRewrite('/docs'), '/docs.html')
  assert.equal(marketingRewrite('/'), '/')
})

// beta.4 unified-shell: the exact clean marketing paths the site + portal now link to must route
// to the marketing origin with the extensionless -> .html rewrite (the .html files remain as compat).
test('unified-shell clean marketing links route to marketing with .html rewrite', () => {
  const clean = {
    '/product': '/product.html', '/platform': '/platform.html', '/use-cases': '/use-cases.html',
    '/security': '/security.html', '/docs': '/docs.html', '/company': '/company.html',
    '/privacy': '/privacy.html', '/request-access': '/request-access.html',
  }
  for (const [inp, out] of Object.entries(clean)) {
    const r = route(inp)
    assert.equal(r.origin, 'marketing', `${inp} should be marketing`)
    assert.equal(r.upstreamPath, out, `${inp} -> ${out}`)
  }
})

// /app/assets/* (JS + CSS) route to the app origin, which serves them with correct MIME types
// (the nginx alias fix). A stale marketing rewrite would break module/stylesheet loading.
test('/app/assets JS and CSS route to the app origin (correct MIME, no marketing rewrite)', () => {
  for (const p of ['/app/assets/index-abc123.js', '/app/assets/index-abc123.css']) {
    const r = route(p)
    assert.equal(r.origin, 'app', `${p} should be app`)
    assert.equal(r.upstreamPath, p, `${p} must not be rewritten`)
  }
})
