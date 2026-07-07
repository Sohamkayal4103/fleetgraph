import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Confines a render error to the main content area so a single page/component failure can NEVER
 * blank the whole app — the header navigation (account state + Sign out) and footer keep working.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Bounded console diagnostic only; no secrets are ever logged.
    console.error('Portal view error:', error.message, info.componentStack?.slice(0, 200))
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <section role="alert" className="view-error">
          <h2>This view could not be displayed</h2>
          <p>Something went wrong rendering this page. Try again or sign out and back in.</p>
          <button type="button" onClick={() => this.setState({ error: null })}>
            Retry
          </button>
        </section>
      )
    }
    return this.props.children
  }
}
