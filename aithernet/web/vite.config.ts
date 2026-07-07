/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard is a static SPA. It talks to the Aithernet API over CORS at the URL given
// by VITE_AITHERNET_API_BASE_URL (default http://127.0.0.1:8080) — it never proxies to or
// reads local fixture files. `base: './'` keeps built asset paths relative so the same
// build also works when served from the backend's /dashboard mount.
export default defineConfig({
  base: './',
  plugins: [react()],
  server: { port: 5173 },
  // Component tests render against jsdom with mocked API responses — no real backend,
  // network, Gemini, Codex, GNU Radio, or Marconi is ever contacted (Part S).
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
