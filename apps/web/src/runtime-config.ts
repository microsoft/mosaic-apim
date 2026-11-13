export type AuthMode = 'entra' | 'local'

export interface RuntimeConfig {
  apiBaseUrl: string
  portalUrl?: string
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
  portalUrl: supplied.portalUrl,
  authMode: supplied.authMode ?? 'local',
  entraTenantId: supplied.entraTenantId ?? 'organizations',
  entraClientId: supplied.entraClientId ?? 'local-development',
  entraApiScope: supplied.entraApiScope ?? 'api://local-development/access_as_user',
  applicationInsightsConnectionString: supplied.applicationInsightsConnectionString,
}

export type PortalDestination =
  | { status: 'ready'; href: string }
  | { status: 'missing' | 'invalid' }

function isLoopback(hostname: string): boolean {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]'
}

export function resolvePortalDestination(value: unknown, consoleOrigin: string): PortalDestination {
  if (value == null || (typeof value === 'string' && !value.trim())) {
    return { status: 'missing' }
  }
  if (typeof value !== 'string') {
    return { status: 'invalid' }
  }
  let destination: URL
  let consoleUrl: URL
  try {
    destination = new URL(value.trim())
    consoleUrl = new URL(consoleOrigin)
  } catch {
    return { status: 'invalid' }
  }
  const localHttp = destination.protocol === 'http:'
    && isLoopback(destination.hostname)
    && isLoopback(consoleUrl.hostname)
  if (
    (destination.protocol !== 'https:' && !localHttp)
    || destination.username
    || destination.password
    || destination.pathname !== '/'
    || destination.search
    || destination.hash
    || destination.origin === consoleUrl.origin
  ) {
    return { status: 'invalid' }
  }
  return { status: 'ready', href: destination.origin }
}
