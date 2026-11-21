import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { ConsoleAccessGate } from '../../../apps/web/src/console-access'
import { MosaicThemeProvider } from '../../../apps/web/src/theme'
import '../../../apps/web/src/index.css'
// The shared shell loads after index.css in the real app too; it carries the tokens and fonts.
import '../../../apps/web/src/prism.css'

createRoot(document.getElementById('root')!).render(
  <MosaicThemeProvider>
    <QueryClientProvider client={new QueryClient()}>
      <ConsoleAccessGate>
        <p>Unexpected administrator content</p>
      </ConsoleAccessGate>
    </QueryClientProvider>
  </MosaicThemeProvider>,
)
