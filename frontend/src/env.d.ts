/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

interface ImportMetaEnv {
  /** Backend API base URL, e.g. `/api/v1`. */
  readonly VITE_API_BASE_URL: string
  /** Public base path for `createWebHistory()`. */
  readonly VITE_APP_BASE_URL: string
  /** Application title. */
  readonly VITE_APP_TITLE: string
  /** Swagger UI URL opened in a new tab. */
  readonly VITE_DOCS_URL: string
  /** Health poll interval in milliseconds (string from env). */
  readonly VITE_HEALTH_POLL_INTERVAL_MS: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}