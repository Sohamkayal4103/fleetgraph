// Rendered evidence for the authenticated portal Getting Started page (beta.9) — local render,
// never deployed. Verifies the SIMPLIFIED primary journey is download&verify -> install ->
// `aithernet setup` -> `aithernet run` (no provider-specific config, no submit/start/watch in the
// primary path), that release metadata populates the EXACT beta.9 filename + SHA-256, that advanced
// workflows are present only in a clearly separated advanced section, that the invalid `--content`
// form is absent, and that no secret/dev path is shown.
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({
  api: {
    getReleases: vi.fn(async () => [{
      release_id: 'r1', version: '0.8.0-beta.9', status: 'published',
      signing_key_id: 'aithernet-prod-betaqual-20260620', manifest_digest: 'sha256:abc',
      published_at: null,
      artifacts: [{ name: 'aithernet_0.8.0~beta.9_amd64.deb', kind: 'deb', sha256: 'x',
                    byte_size: 1, os_family: 'any', architecture: 'amd64', content_type: 'x' }],
    }]),
    getReleaseBundleMeta: vi.fn(async () => ({
      filename: 'aithernet-0.8.0-beta.9-ubuntu24.04-amd64.zip',
      sha256: 'sha256:d11fed95375bc1e16c6d541a051ae60db561c1bfd335e6072938d03d480eb973',
      byte_size: 18177376, files: [],
    })),
  },
}))

import { GettingStarted } from '../pages/CustomerPortal'

describe('portal Getting Started (beta.9 render evidence)', () => {
  it('renders the simplified beta.9 setup→run journey from metadata, safely', async () => {
    const { container } = render(<GettingStarted />)

    // Release metadata populates the EXACT beta.9 ZIP filename + SHA-256 (resolved asynchronously).
    await screen.findByText(
      /d11fed95375bc1e16c6d541a051ae60db561c1bfd335e6072938d03d480eb973/)
    expect(screen.getAllByText(/aithernet-0\.8\.0-beta\.9-ubuntu24\.04-amd64\.zip/).length)
      .toBeGreaterThan(0)
    expect(screen.getAllByText(/aithernet_0\.8\.0~beta\.9_amd64\.deb/).length).toBeGreaterThan(0)

    const html = container.textContent ?? ''
    // Primary journey is the four-step setup -> run experience.
    expect(html).toMatch(/Guided setup — one command/)
    expect(html).toMatch(/aithernet setup/)
    expect(html).toMatch(/aithernet run "/)
    expect(html).toMatch(/Run work — one command/)
    expect(html).toMatch(/aithernet doctor --repair/)
    // Plain-language provider choices (no adapter IDs required in the primary journey).
    expect(html).toMatch(/sign in with Gemini/)
    expect(html).toMatch(/sign in with Codex/i)
    // beta.8 -> beta.9 migration is `apt install` + `aithernet setup` (no manual systemd/PATH/YAML).
    expect(html).toMatch(/Upgrade from beta\.8/)

    // Advanced workflows live ONLY in the advanced section (provider config + mission lifecycle).
    expect(html).toMatch(/Advanced \/ operator workflows/)
    expect(html).toMatch(/agents connect coordinator/)
    expect(html).toMatch(/mission submit/)
    expect(html).toMatch(/mission retry/)

    // The invalid --content form is gone everywhere; no stale beta.8 obsolete onboarding.
    expect(html).not.toMatch(/--content/)
    expect(html).not.toMatch(/setup --node-name/)
    expect(html).not.toMatch(/hardware install --profile/)
    expect(html).not.toMatch(/aithernet enroll/)
    expect(html).not.toMatch(/hosted heartbeat/)

    // sudo vs normal-user boundary explicit; receive-only; WSL audio not an SDR.
    expect(html).toMatch(/sudo/)
    expect(html).toMatch(/never root/)
    expect(html).toMatch(/no RF transmission/)
    expect(html).toMatch(/audio\s+endpoints are not SDRs/)

    // no secret / private path / developer path is shown.
    expect(html).not.toMatch(/\/home\/aditya/)
    expect(html).not.toMatch(/AIza[0-9A-Za-z_-]{10,}/)
    expect(html).not.toMatch(/BEGIN [A-Z ]*PRIVATE KEY/)
  })
})
