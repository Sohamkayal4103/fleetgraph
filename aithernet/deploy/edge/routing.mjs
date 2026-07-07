// Aithernet canonical-origin edge routing POLICY (single source of truth).
//
// The production edge is the Caddy reverse proxy reached through the Cloudflare Tunnel
// (deploy/reverse-proxy/Caddyfile.cloudflare). This module encodes the exact path -> origin
// allowlist, method rules, marketing URL rewrite and inbound-header strip-list that the Caddy
// config implements, so the routing decisions are deterministically unit-testable independently of
// a live Caddy — and are drop-in reusable if a Cloudflare Worker is ever preferred over Caddy.
//
// Two fixed origins ONLY (closed allowlist — no open proxy, no user-controlled destination):
//   * 'app'       -> the internal container stack (portal SPA / control-plane / ingestion),
//                    reached on the tunnel origin. Owns auth, the portal, and the same-origin API.
//   * 'marketing' -> the static public site (GitHub Pages, repo aithernet-site). Everything else.
//
// The administrator origin (admin.aithernet.online) is intentionally NOT reachable from www — it is
// a separate Cloudflare-Access-protected browser origin. This module never routes to it.

/** Path prefixes served by the APP origin, matched on a path-segment boundary (so `/apparel`
 *  does NOT match `/app`). `/api` is deliberately excluded here: only `/api/...` is the API; a bare
 *  `/api` is the marketing API reference page. */
export const APP_BASES = Object.freeze([
  '/app', // SPA shell + all customer portal deep links + /app/accept-invite + /app/assets/*
  '/login',
  '/logout',
  '/status',
  '/reset',
  '/accept-invite', // legacy/direct invite path; the SPA shim forwards it to /app/accept-invite
  '/v1/downloads', // authenticated + public artifact downloads (node + browser)
  '/v1/public', // public release metadata / verification keys
  '/v1/node', // signed node enrollment / heartbeat / config / release (programmatic)
  '/health', // readiness / diagnostics probes
  '/ingest', // telemetry / artifact ingestion
])

/** Inbound request headers that a client could spoof to forge its identity or origin. The edge
 *  MUST strip these before proxying; the trusted client IP is Cloudflare's CF-Connecting-IP and the
 *  origin is loopback-only (reachable only by cloudflared). */
export const STRIP_INBOUND_HEADERS = Object.freeze([
  'x-forwarded-for',
  'x-forwarded-host',
  'x-forwarded-proto',
  'x-forwarded-port',
  'x-real-ip',
  'forwarded',
])

function underBase(path, base) {
  return path === base || path.startsWith(base + '/')
}

/** True iff `path` is served by the internal APP origin (portal/control-plane/ingestion). */
export function isAppPath(path) {
  if (typeof path !== 'string' || !path.startsWith('/')) return false
  // The control-plane API lives strictly under /api/... — a bare /api is the marketing page.
  if (path.startsWith('/api/')) return true
  return APP_BASES.some((b) => underBase(path, b))
}

/**
 * Decide the origin and (for marketing) the rewritten upstream path for a request.
 * @param {string} rawPath  window.location.pathname (NO query string).
 * @param {string} method   HTTP method (defaults GET).
 * @returns {{origin:'app'|'marketing', upstreamPath:string, allow:boolean, status?:number}}
 *   `allow:false` with a `status` means the edge should short-circuit (e.g. 405 for a mutating
 *   method on the static marketing origin). Query strings are always preserved by the caller.
 */
export function route(rawPath, method = 'GET') {
  const path = normalizePath(rawPath)
  const m = (method || 'GET').toUpperCase()
  if (isAppPath(path)) {
    // The app origin preserves method + body; the container stack enforces its own method rules
    // (e.g. 405 for a mutating call on a read-only route) and CSRF.
    return { origin: 'app', upstreamPath: path, allow: true }
  }
  // Marketing is a STATIC site: only safe methods are meaningful. Anything else is a deterministic
  // 405 at the edge (never silently downgraded, never proxied as a write to a static host).
  if (m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS') {
    return { origin: 'marketing', upstreamPath: path, allow: false, status: 405 }
  }
  return { origin: 'marketing', upstreamPath: marketingRewrite(path), allow: true }
}

/** Collapse a path to a safe canonical form: strip `..`/`.` segments and duplicate slashes so an
 *  encoded/dot-segment path cannot escape the allowlist (defence-in-depth; the edge also normalizes). */
export function normalizePath(rawPath) {
  let path = typeof rawPath === 'string' && rawPath.startsWith('/') ? rawPath : '/' + (rawPath || '')
  const out = []
  for (const seg of path.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') {
      out.pop()
      continue
    }
    out.push(seg)
  }
  const trailing = path.length > 1 && path.endsWith('/') ? '/' : ''
  return '/' + out.join('/') + (out.length ? trailing : '')
}

/** Map the spec's clean, extensionless marketing routes to the GitHub Pages `.html` files, while
 *  leaving `/`, directory paths and already-extensioned assets untouched. `/product` -> `/product.html`. */
export function marketingRewrite(path) {
  if (path === '/' || path.endsWith('/')) return path
  const last = path.slice(path.lastIndexOf('/') + 1)
  if (last.includes('.')) return path // already has an extension (asset, or /api.html)
  return path + '.html'
}
