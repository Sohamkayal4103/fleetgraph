// Real-browser acceptance for the one-click invitation onboarding journey (Stage 14F).
//
// Admin (one context) creates a policy + invitation and reads the COMPLETE link from the
// Development Email Preview. A SEPARATE unauthenticated browser context opens that link and
// completes onboarding. Proves the canonical link reaches the public accept route (not #/portal),
// the token is auto-read (no manual field), the invited email is read-only, policies are accepted,
// the account is created + authenticated, refresh persists, and authorization + negative-token
// cases hold. Temporary credentials/state only.
//
// Env: BASE_URL, PORTAL_ADMIN_EMAIL, PORTAL_ADMIN_PASSWORD

import puppeteer from 'puppeteer'

const BASE = process.env.BASE_URL || 'http://localhost:8080'
const ADMIN_EMAIL = process.env.PORTAL_ADMIN_EMAIL
const ADMIN_PW = process.env.PORTAL_ADMIN_PASSWORD
const INVITEE = 'invitee@local.invalid'
const INVITEE_PW = 'Invitee-pass-123'

const out = { errors: [] }
const fail = (k) => {
  out.failed = k
  console.log(JSON.stringify(out, null, 2))
  process.exit(1)
}

const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] })
try {
  // ---- admin context: sign in, publish a policy, create an invitation, read the link ----
  const adminCtx = await browser.createBrowserContext()
  const admin = await adminCtx.newPage()
  await admin.goto(`${BASE}/#/signin`, { waitUntil: 'networkidle0' })
  await admin.type('#signin-email', ADMIN_EMAIL)
  await admin.type('#signin-password', ADMIN_PW)
  await Promise.all([admin.click('button[type=submit]'), admin.waitForNetworkIdle({ idleTime: 700 })])

  const api = async (path, method = 'GET', body) =>
    admin.evaluate(
      async (b, p, m, bd) => {
        const csrf = document.cookie.split(';').map((s) => s.trim()).find((s) => s.startsWith('aithernet_hosted_csrf='))
        const token = csrf ? decodeURIComponent(csrf.split('=').slice(1).join('=')) : ''
        const r = await fetch(`${b}/api${p}`, {
          method: m,
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
          body: bd ? JSON.stringify(bd) : undefined,
        })
        return { status: r.status, json: await r.json().catch(() => null) }
      },
      BASE, path, method, body,
    )

  await api('/v1/policies', 'POST', {
    policy_type: 'preview_terms',
    version: `inv-${Date.now()}`,
    title: 'Invite Preview Terms',
    document_text: 'local test fixture',
    required_categories: ['operational'],
  })
  const invResp = await api('/v1/invitations', 'POST', { email: INVITEE, proposed_tenant_name: 'Invite Tenant' })
  out.invitation_created = invResp.status === 200
  const preview = (await api('/v1/admin/email-preview')).json
  const msg = preview.messages.find((m) => m.to === INVITEE && m.kind === 'invitation')
  out.preview_has_link = Boolean(msg && msg.link)
  out.link_is_hash_route = Boolean(msg && /\/#\/accept-invite\?token=/.test(msg.link) && !msg.link.includes('/#/portal'))
  const link = msg.link
  const relLink = link.replace(/^https?:\/\/[^/]+/, '') // strip origin so it works on any port

  // ---- fresh unauthenticated context: open the invitation link ----
  const custCtx = await browser.createBrowserContext()
  const cust = await custCtx.newPage()
  await cust.goto(`${BASE}${relLink}`, { waitUntil: 'networkidle0' })
  await new Promise((r) => setTimeout(r, 700))
  out.landed_hash = await cust.evaluate(() => location.hash)
  out.on_accept_route = out.landed_hash.startsWith('#/accept-invite')
  out.not_on_portal = !out.landed_hash.startsWith('#/portal')
  out.no_manual_token_field = await cust.evaluate(() => !document.querySelector('#inv-token'))
  out.token_scrubbed_from_url = !out.landed_hash.includes('token=')
  const bodyText = await cust.evaluate(() => document.body.innerText)
  out.invited_email_shown = bodyText.includes(INVITEE)
  // The invited email is read-only: there is no editable input pre-filled with it.
  out.email_not_editable = await cust.evaluate((e) => {
    return !Array.from(document.querySelectorAll('input')).some((i) => i.value === e)
  }, INVITEE)
  out.no_token_in_storage = await cust.evaluate(
    () => window.localStorage.length === 0 && window.sessionStorage.length === 0,
  )

  // Complete onboarding: password + confirm + accept policy.
  await cust.type('#inv-password', INVITEE_PW)
  await cust.type('#inv-confirm', INVITEE_PW)
  const cb = await cust.$('input[type=checkbox]')
  if (cb) await cb.click()
  await Promise.all([cust.click('button[type=submit]'), cust.waitForNetworkIdle({ idleTime: 800 })])
  await new Promise((r) => setTimeout(r, 800))

  out.session_after_accept = await cust.evaluate(async (b) => {
    const r = await fetch(`${b}/api/v1/auth/session`, { credentials: 'include' })
    return r.status
  }, BASE)
  const afterHash = await cust.evaluate(() => location.hash)
  out.redirected_to_portal = afterHash.startsWith('#/portal')
  const portalText = await cust.evaluate(() => document.body.innerText)
  out.portal_renders = portalText.includes('Customer overview') || portalText.includes('Tenant')
  out.no_signin_required = !/Sign in required/i.test(portalText)
  out.nav_signed_in = await cust.evaluate(() =>
    !Array.from(document.querySelectorAll('header a, nav a')).some((a) => a.textContent.trim() === 'Sign in'),
  )

  // Accepted policy visible.
  const polText = await (async () => {
    await cust.evaluate(() => (location.hash = '#/portal/policies'))
    await new Promise((r) => setTimeout(r, 600))
    return cust.evaluate(() => document.body.innerText)
  })()
  out.accepted_policy_visible = /accepted/i.test(polText)

  // Refresh preserves the authenticated session.
  await cust.reload({ waitUntil: 'networkidle0' })
  await new Promise((r) => setTimeout(r, 500))
  out.session_after_refresh = await cust.evaluate(async (b) => {
    const r = await fetch(`${b}/api/v1/auth/session`, { credentials: 'include' })
    return r.status
  }, BASE)

  // Authorization: the customer cannot open admin routes.
  await cust.evaluate(() => (location.hash = '#/admin'))
  await new Promise((r) => setTimeout(r, 500))
  out.customer_blocked_from_admin = /Not authorized/i.test(await cust.evaluate(() => document.body.innerText))

  // Reused token: the SAME link in a fresh context is rejected as consumed.
  const reuseCtx = await browser.createBrowserContext()
  const reuse = await reuseCtx.newPage()
  await reuse.goto(`${BASE}${relLink}`, { waitUntil: 'networkidle0' })
  await new Promise((r) => setTimeout(r, 600))
  out.reused_token_rejected = /already been used/i.test(await reuse.evaluate(() => document.body.innerText))

  // Altered token: rejected as invalid.
  const altCtx = await browser.createBrowserContext()
  const alt = await altCtx.newPage()
  await alt.goto(`${BASE}${relLink}x`, { waitUntil: 'networkidle0' })
  await new Promise((r) => setTimeout(r, 600))
  out.altered_token_rejected = /invalid or has been altered/i.test(await alt.evaluate(() => document.body.innerText))

  // ---- assertions ----
  const must = [
    'invitation_created', 'preview_has_link', 'link_is_hash_route', 'on_accept_route', 'not_on_portal',
    'no_manual_token_field', 'token_scrubbed_from_url', 'invited_email_shown', 'email_not_editable',
    'no_token_in_storage', 'redirected_to_portal', 'portal_renders', 'no_signin_required',
    'nav_signed_in', 'accepted_policy_visible', 'customer_blocked_from_admin', 'reused_token_rejected',
    'altered_token_rejected',
  ]
  for (const k of must) if (out[k] !== true) fail(k)
  if (out.session_after_accept !== 200) fail('session_after_accept_not_200')
  if (out.session_after_refresh !== 200) fail('session_after_refresh_not_200')

  out.ok = true
  console.log(JSON.stringify(out, null, 2))
} finally {
  await browser.close()
}
