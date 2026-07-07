import { useCallback, useEffect, useState } from 'react'

/**
 * Loads async data and exposes the canonical states for the UI: `data===null && error===null`
 * means loading; `error` set means failure; otherwise `data` is ready. `reload()` re-fetches.
 * API errors (the client throws `{status, message}`) are normalized to a readable message; a 403
 * surfaces as a permission message so a panel shows "permission denied" rather than blanking.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(() => {
    setError(null)
    setData(null)
    fn().then(
      (d) => setData(d),
      (e: unknown) => {
        const err = e as { status?: number; message?: string }
        if (err.status === 403) setError('You do not have permission to view this.')
        else setError(err.message ?? 'Could not load this section.')
      },
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    reload()
  }, [reload])

  return { data, error, reload, setData }
}

/** Run a mutating action, surfacing a success or error banner message. */
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  async function run<T>(fn: () => Promise<T>, okText: string | ((result: T) => string)) {
    setBusy(true)
    setMessage(null)
    try {
      const result = await fn()
      setMessage({ kind: 'success', text: typeof okText === 'function' ? okText(result) : okText })
      return true
    } catch (e) {
      const err = e as { message?: string }
      setMessage({ kind: 'error', text: err.message ?? 'Action failed.' })
      return false
    } finally {
      setBusy(false)
    }
  }
  return { busy, message, run, setMessage }
}
