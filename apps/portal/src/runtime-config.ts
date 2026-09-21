export type AuthMode = 'entra' | 'local'

export interface RuntimeConfig {
  apiBaseUrl: string
  authMode: AuthMode
  entraTenantId: string
  entraClientId: string
  entraApiScope: string
  applicationInsightsConnectionString?: string
}

declare global {
  interface Window {
    __MOSAIC_CONFIG__?: Partial<RuntimeConfig>
  }
}

const supplied = window.__MOSAIC_CONFIG__ ?? {}

export const runtimeConfig: RuntimeConfig = {
  apiBaseUrl: supplied.apiBaseUrl ?? 'http://localhost:8000',
  authMode: supplied.authMode ?? 'local',
  entraTenantId: supplied.entraTenantId ?? 'organizations',
  entraClientId: supplied.entraClientId ?? 'local-development',
  entraApiScope: supplied.entraApiScope ?? 'api://local-development/access_as_user',
  applicationInsightsConnectionString: supplied.applicationInsightsConnectionString,
}
