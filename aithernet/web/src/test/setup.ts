// Vitest setup: jest-dom matchers + a clipboard stub so component tests never touch a real
// backend, network, or browser API. Every test mocks `api` (the only I/O boundary).
import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// jsdom has no clipboard; stub it so IdTag copy buttons are testable.
Object.assign(navigator, { clipboard: { writeText: () => Promise.resolve() } })
