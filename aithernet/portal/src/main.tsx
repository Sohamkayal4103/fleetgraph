import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { SessionProvider } from './session'
import { RouterProvider } from './routes/router'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <RouterProvider>
      <SessionProvider>
        <App />
      </SessionProvider>
    </RouterProvider>
  </React.StrictMode>,
)
