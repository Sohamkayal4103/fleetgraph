import { useCallback, useEffect, useState } from 'react'
import { ApiError, type ApiErrorCategory } from './client'

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

export function errorCategory(err: unknown): ApiErrorCategory | null {
  return err instanceof ApiError ? err.category : null
}

export interface AsyncState<T> {
  data: T | null
  error: string | null
  /** The failure category, when the error came from the API client (else null). */
  category: ApiErrorCategory | null
  loading: boolean
  /** Force a refetch. */
  reload: () => void
}

/**
 * Load an async resource and track {data, error, loading}. Refetches whenever any value in
 * `deps` changes or `reload()` is called. Stale results from a superseded load are ignored.
 */
export function useResource<T>(loader: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [category, setCategory] = useState<ApiErrorCategory | null>(null)
  const [loading, setLoading] = useState(false)
  const [tick, setTick] = useState(0)

  const reload = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let active = true
    setLoading(true)
    loader()
      .then((result) => {
        if (active) {
          setData(result)
          setError(null)
          setCategory(null)
        }
      })
      .catch((err) => {
        if (active) {
          setError(errorMessage(err))
          setCategory(errorCategory(err))
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])

  return { data, error, category, loading, reload }
}

/**
 * Periodically call `reload` on a BOUNDED interval while `enabled`. The interval is clamped to
 * a sane floor so a misconfigured caller can never spin a tight uncontrolled polling loop, and
 * the timer is always cleared on unmount or when disabled. Reads triggered this way are
 * side-effect free — they never send a message or resume a mission.
 */
export function useAutoReload(reload: () => void, intervalMs: number, enabled = true): void {
  useEffect(() => {
    if (!enabled) return
    const bounded = Math.max(2000, intervalMs)
    const id = setInterval(reload, bounded)
    return () => clearInterval(id)
  }, [reload, intervalMs, enabled])
}

/** Parse a JSON object from text, returning either the value or a human error message. */
export function parseJsonObject(
  text: string,
): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  const trimmed = text.trim()
  if (trimmed === '') return { ok: true, value: {} }
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch (err) {
    return { ok: false, error: `Invalid JSON: ${(err as Error).message}` }
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: 'Must be a JSON object (e.g. {"key": "value"}).' }
  }
  return { ok: true, value: parsed as Record<string, unknown> }
}
