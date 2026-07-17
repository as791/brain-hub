/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BRAINHUB_API_URL?: string
  readonly VITE_BRAINHUB_WS_URL?: string
  readonly VITE_BRAINHUB_DEMO?: 'force' | 'fallback' | 'off'
  /** Development only. Production tokens must be entered at runtime. */
  readonly VITE_BRAINHUB_API_TOKEN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
