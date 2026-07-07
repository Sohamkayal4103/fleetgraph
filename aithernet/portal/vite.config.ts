/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The portal is a static SPA served under the canonical origin https://www.aithernet.online. It
// talks to the control plane SAME-ORIGIN at the relative /api base (VITE_CONTROL_PLANE_BASE_URL,
// default /api). It never proxies to or reads local fixture files, and never executes
// SDR/mission workloads.
export default defineConfig({
  // Fixed absolute asset base. The SPA shell is served at several www paths (/app, /app/*,
  // /login, /logout, /status, /app/accept-invite); with an absolute /app/ base every shell loads
  // its hashed assets from /app/assets/*, which cannot collide with the marketing site's /assets/*
  // and lets the edge route the whole /app/ prefix to the app origin. No <base href> is emitted,
  // so form actions and relative fetches resolve against the real pathname.
  base: '/app/',
  plugins: [react()],
  server: { port: 5273 },
  // Component/unit tests render against jsdom with mocked fetch responses — no real backend,
  // network, or hardware is ever contacted.
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
