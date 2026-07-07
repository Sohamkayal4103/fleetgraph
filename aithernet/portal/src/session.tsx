import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from './api/client'
import type { Session } from './api/types'

interface SessionContextValue {
  session: Session | null
  loading: boolean
  refresh: () => Promise<void>
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
}

const EMPTY: Session = { authenticated: false, user: null, csrf_token: null }

const SessionContext = createContext<SessionContextValue>({
  session: null,
  loading: true,
  refresh: async () => {},
  signIn: async () => {},
  signOut: async () => {},
})

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const s = await api.getSession()
      setSession(s)
    } catch {
      setSession(EMPTY)
    } finally {
      setLoading(false)
    }
  }, [])

  const signIn = useCallback(
    async (email: string, password: string) => {
      await api.login(email, password)
      await refresh()
    },
    [refresh],
  )

  const signOut = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      setSession(EMPTY)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <SessionContext.Provider value={{ session, loading, refresh, signIn, signOut }}>
      {children}
    </SessionContext.Provider>
  )
}

export function useSession() {
  return useContext(SessionContext)
}
