import { PublicClientApplication, type Configuration } from '@azure/msal-browser'
import { runtimeConfig } from './runtime-config'

const configuration: Configuration = {
  auth: {
    clientId: runtimeConfig.entraClientId,
    authority: `https://login.microsoftonline.com/${runtimeConfig.entraTenantId}`,
    redirectUri: window.location.origin,
    postLogoutRedirectUri: window.location.origin,
  },
  cache: {
    cacheLocation: 'sessionStorage',
  },
}

export const msalInstance = new PublicClientApplication(configuration)
