// Vitest setup: jest-dom matchers. Every test mocks `fetch` (the only network boundary); no
// real backend, network, or hardware is ever contacted.
import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
