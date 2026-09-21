import { ApplicationInsights } from '@microsoft/applicationinsights-web'
import { MsalProvider } from '@azure/msal-react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { msalInstance } from './msal'
import { runtimeConfig } from './runtime-config'

if (runtimeConfig.applicationInsightsConnectionString) {
  const insights = new ApplicationInsights({
    config: {
      connectionString: runtimeConfig.applicationInsightsConnectionString,
      enableAutoRouteTracking: true,
    },
  })
  insights.loadAppInsights()
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 15_000 },
    mutations: { retry: false },
  },
})

void msalInstance.initialize().then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <MsalProvider instance={msalInstance}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </MsalProvider>
    </StrictMode>,
  )
})
