import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { ConsoleAccessGate } from '../../../apps/web/src/console-access'
import { MosaicThemeProvider } from '../../../apps/web/src/theme'
import '../../../apps/web/src/index.css'

createRoot(document.getElementById('root')!).render(
  <MosaicThemeProvider>
    <QueryClientProvider client={new QueryClient()}>
      <ConsoleAccessGate>
        <p>Unexpected administrator content</p>
      </ConsoleAccessGate>
    </QueryClientProvider>
  </MosaicThemeProvider>,
)
