// Real-browser acceptance for the hosted portal auth flow (Stage 14F).
//
// Drives a headless Chromium through the browser-facing reverse proxy and proves the full
// authenticated lifecycle. Uses TEMPORARY credentials + state injected via env (never the
// persistent administrator secret):
//   BASE_URL, PORTAL_ADMIN_EMAIL, PORTAL_ADMIN_PASSWORD
// Prints a single JSON result line; exits non-zero on any failed assertion.

import puppeteer from 'puppeteer'

const BASE = process.env.BASE_URL || 'http://localhost:8080'
const EMAIL = process.env.PORTAL_ADMIN_EMAIL
const PASSWORD = process.env.PORTAL_ADMIN_PASSWORD
if (!EMAIL || !PASSWORD) {
  console.error('PORTAL_ADMIN_EMAIL / PORTAL_ADMIN_PASSWORD required')
  process.exit(2)
}

const out = {}
const fail = (k) => {
  out.failed = k
  console.log(JSON.stringify(out, null, 2))
  process.exit(1)
}

const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] })
try {
  const page = await browser.newPage()
  const status = {}
  page.on('response', (r) => {
    if (r.url().includes('/api/v1/auth/login')) status.login = r.status()
    if (r.url().includes('/api/v1/auth/session')) status.session = r.status()
  })

  const navHasSignIn = () =>
    page.evaluate(() =>
      Array.from(document.querySelectorAll('header a, nav a')).some(
        (a) => a.textContent.trim() === 'Sign in',
      ),
    )

  // 1) Load the portal origin first (so in-page fetches are same-origin), then confirm the
  //    session endpoint is unauthenticated before login.
  await page.goto(`${BASE}/#/signin`, { waitUntil: 'networkidle0' })
  out.session_before_login = await page.evaluate(async (b) => {
    const r = await fetch(`${b}/api/v1/auth/session`, { credentials: 'include' })
    return r.status
  }, BASE)

  // 2) Sign in through the form.
  await page.type('#signin-email', EMAIL)
  await page.type('#signin-password', PASSWORD)
  await Promise.all([page.click('button[type=submit]'), page.waitForNetworkIdle({ idleTime: 800 })])
  await new Promise((r) => setTimeout(r, 500))
  out.login_status = status.login
  out.session_status_after_login = status.session

  // 3) Browser stored the HttpOnly session cookie + the readable CSRF cookie.
  const cookies = await page.cookies()
  out.session_cookie_httponly = cookies.some((c) => c.name === 'aithernet_hosted_session' && c.httpOnly)
  out.csrf_cookie_present = cookies.some((c) => c.name === 'aithernet_hosted_csrf' && !c.httpOnly)

  // 3b) Admin console + a public page render meaningful, non-placeholder content.
  const adminText = await page.evaluate(async (b) => {
    location.hash = '#/admin'
    await new Promise((r) => setTimeout(r, 600))
    return document.body.innerText
  }, BASE)
  out.admin_overview_renders = adminText.includes('Admin overview') && adminText.includes('Tenants')
  out.no_placeholder_in_admin = !adminText.includes('PLACEHOLDER:')
  const publicText = await page.evaluate(async () => {
    location.hash = '#/install'
    await new Promise((r) => setTimeout(r, 500))
    return document.body.innerText
  })
  out.public_install_renders = publicText.includes('aithernet enroll') && !publicText.includes('PLACEHOLDER:')

  // 4) Portal renders authenticated; nav no longer shows Sign in; admin view available.
  await page.goto(`${BASE}/#/portal`, { waitUntil: 'networkidle0' })
  await new Promise((r) => setTimeout(r, 400))
  const portalText = await page.evaluate(() => document.body.innerText)
  out.nav_shows_sign_in_after_login = await navHasSignIn()
  out.portal_requires_signin = /Sign in required/i.test(portalText)
  out.account_email_shown = portalText.includes(EMAIL)
  out.admin_link_present = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a')).some((a) => a.textContent.includes('Admin portal')),
  )

  // 5) Hard refresh restores the session from the HttpOnly cookie.
  await page.reload({ waitUntil: 'networkidle0' })
  await new Promise((r) => setTimeout(r, 400))
  out.nav_shows_sign_in_after_refresh = await navHasSignIn()
  out.portal_requires_signin_after_refresh = /Sign in required/i.test(
    await page.evaluate(() => document.body.innerText),
  )

  // 6) CSRF-protected write: rejected without token, accepted with the token.
  const csrf = await page.evaluate(() => {
    const m = document.cookie.split(';').map((s) => s.trim()).find((s) => s.startsWith('aithernet_hosted_csrf='))
    return m ? decodeURIComponent(m.split('=').slice(1).join('=')) : null
  })
  out.csrf_write_without_token = await page.evaluate(async (b) => {
    const r = await fetch(`${b}/api/v1/invitations`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: 'bt-csrf@local.invalid', proposed_tenant_name: 'BT CSRF' }),
    })
    return r.status
  }, BASE)
  out.csrf_write_with_token = await page.evaluate(
    async (b, token) => {
      const r = await fetch(`${b}/api/v1/invitations`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
        body: JSON.stringify({ email: 'bt-csrf2@local.invalid', proposed_tenant_name: 'BT CSRF2' }),
      })
      return r.status
    },
    BASE, csrf,
  )

  // 7) Log out via the global nav; session becomes unauthenticated; protected route requires sign-in.
  await page.goto(`${BASE}/#/portal`, { waitUntil: 'networkidle0' })
  await page.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button')).find(
      (b) => b.textContent.trim() === 'Sign out',
    )
    if (btn) btn.click()
  })
  await new Promise((r) => setTimeout(r, 600))
  out.session_after_logout = await page.evaluate(async (b) => {
    const r = await fetch(`${b}/api/v1/auth/session`, { credentials: 'include' })
    return r.status
  }, BASE)
  await page.goto(`${BASE}/#/portal`, { waitUntil: 'networkidle0' })
  await new Promise((r) => setTimeout(r, 300))
  out.nav_shows_sign_in_after_logout = await navHasSignIn()
  out.portal_requires_signin_after_logout = /Sign in required/i.test(
    await page.evaluate(() => document.body.innerText),
  )

  // ---- assertions ----
  if (out.session_before_login !== 401) fail('session_before_login_not_401')
  if (out.login_status !== 200) fail('login_not_200')
  if (out.session_status_after_login !== 200) fail('session_after_login_not_200')
  if (!out.session_cookie_httponly) fail('session_cookie_not_httponly')
  if (!out.csrf_cookie_present) fail('csrf_cookie_absent')
  if (out.nav_shows_sign_in_after_login) fail('nav_still_signin_after_login')
  if (out.portal_requires_signin) fail('portal_requires_signin_after_login')
  if (!out.account_email_shown) fail('account_email_not_shown')
  if (!out.admin_link_present) fail('admin_link_absent_for_platform_admin')
  if (!out.admin_overview_renders) fail('admin_overview_not_rendered')
  if (!out.no_placeholder_in_admin) fail('placeholder_in_admin')
  if (!out.public_install_renders) fail('public_install_not_rendered')
  if (out.nav_shows_sign_in_after_refresh) fail('nav_signin_after_refresh')
  if (out.portal_requires_signin_after_refresh) fail('portal_requires_signin_after_refresh')
  if (out.csrf_write_without_token !== 403) fail('csrf_not_rejected_without_token')
  if (out.csrf_write_with_token !== 200) fail('csrf_not_accepted_with_token')
  if (out.session_after_logout !== 401) fail('session_not_401_after_logout')
  if (!out.nav_shows_sign_in_after_logout) fail('nav_not_signin_after_logout')
  if (!out.portal_requires_signin_after_logout) fail('portal_not_requiring_signin_after_logout')

  out.ok = true
  console.log(JSON.stringify(out, null, 2))
} finally {
  await browser.close()
}
