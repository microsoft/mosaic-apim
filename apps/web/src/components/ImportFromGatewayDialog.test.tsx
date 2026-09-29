import { Button } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Gateway, ModelApiCandidateList } from '../types'
import { ImportFromGatewayDialog } from './ImportFromGatewayDialog'

const api = { listGateways: vi.fn(), listImportableApis: vi.fn(), importModelApis: vi.fn() }
vi.mock('../api', () => ({ useMosaicApi: () => api }))

const gateway: Gateway = {
  id: 'gateway_1',
  tenantId: 'tenant-test',
  name: 'Test gateway',
  provider: 'apim',
  azureResourceId:
    '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-test' +
    '/providers/Microsoft.ApiManagement/service/apim-test',
  subscriptionId: '00000000-0000-0000-0000-000000000000',
  resourceGroup: 'rg-test',
  serviceName: 'apim-test',
  environmentLabel: 'dev',
  managementMode: 'observe',
  status: 'connected',
  access: {
    canRead: true,
    canWrite: false,
    evaluation: 'effectivePermissions',
    checkedAt: '2026-09-01T12:00:00Z',
    missingActions: [],
    remediation: null,
    message: 'MOSAIC can read this gateway.',
  },
  capabilities: {
    skuName: 'Developer',
    skuCapacity: 1,
    provisioningState: 'Succeeded',
    location: 'eastus2',
    gatewayUrl: 'https://apim-test.example.test',
    managementApiVersion: '2024-05-01',
    aiGatewayPolicies: 'available',
    mcpServers: 'available',
    identityObserved: true,
    notes: [],
  },
  inventory: {
    apis: 2,
    aiApis: 1,
    mcpServers: 0,
    operations: 2,
    products: 0,
    subscriptions: 0,
    users: 0,
    groups: 0,
    backends: 0,
    namedValues: 0,
    policyDocuments: 0,
    policyFragments: 0,
    recognizedFacets: 0,
    unrecognizedFacets: 0,
    mosaicManagedFacets: 0,
  },
  lastSyncedAt: '2026-09-01T12:05:00Z',
  lastSyncError: null,
  createdAt: '2026-09-01T11:00:00Z',
  updatedAt: '2026-09-01T12:05:00Z',
}

const importable: ModelApiCandidateList = {
  gatewayId: gateway.id,
  snapshotId: 'snapshot_1',
  lastSyncedAt: gateway.lastSyncedAt,
  candidates: [
    {
      apiName: 'chat-api',
      displayName: 'Chat completions',
      path: 'openai',
      serviceUrl: 'https://models.example.test/openai',
      aiKind: 'azureOpenAi',
      aiSignals: ['Backend URL points at Azure OpenAI.'],
      operationCount: 1,
      productNames: [],
      recommended: true,
      alreadyImported: false,
    },
    {
      apiName: 'echo-api',
      displayName: 'Echo',
      path: 'echo',
      serviceUrl: 'https://echo.example.test',
      aiKind: 'none',
      aiSignals: [],
      operationCount: 1,
      productNames: [],
      recommended: false,
      alreadyImported: false,
    },
  ],
}

const failure = 'The gateway changed since its last sync. Sync it, then import again.'

// A page that opens the dialog, as the models page does.
function renderDialog() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Page() {
    const [open, setOpen] = useState(true)
    return (
      <>
        <Button onClick={() => setOpen(true)}>Import from a gateway</Button>
        <ImportFromGatewayDialog
          kind="apis"
          open={open}
          initialGatewayId={gateway.id}
          onClose={() => setOpen(false)}
          onImported={() => {}}
        />
      </>
    )
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <Page />
    </QueryClientProvider>,
  )
}

// The element that has focus, which has to be inside the open dialog: Fluent closes a dialog on Escape
// only when the key is pressed inside it. The dialog itself contains everything, so it doesn't count.
function focused() {
  const element = document.activeElement as HTMLElement
  const dialog = screen.getByRole('dialog')
  expect(element).not.toBe(document.body)
  expect(element).not.toBe(dialog)
  expect(dialog).toContainElement(element)
  return element
}

async function expectClosed() {
  // The page stays aria-hidden for a moment after a modal closes.
  await screen.findByRole('button', { name: 'Import from a gateway' })
  expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
}

describe('ImportFromGatewayDialog', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.listGateways.mockResolvedValue([gateway])
    api.listImportableApis.mockResolvedValue(importable)
    api.importModelApis.mockResolvedValue([])
  })

  it('keeps focus on Import while it works, without letting it be pressed again', async () => {
    const user = userEvent.setup()
    api.importModelApis.mockReturnValue(new Promise(() => {}))
    renderDialog()

    await user.click(await screen.findByRole('button', { name: 'Import 1' }))

    // A browser takes focus off a button that becomes disabled, so a busy button stays focusable instead.
    const importing = await screen.findByRole('button', { name: 'Importing...' })
    expect(importing).toHaveFocus()
    expect(importing).toHaveAttribute('aria-disabled', 'true')
    expect(importing).not.toBeDisabled()
    await user.click(importing)
    await user.keyboard('{Enter}')
    expect(api.importModelApis).toHaveBeenCalledExactlyOnceWith(gateway.id, ['chat-api'])
  })

  it('closes on Escape while the import is in flight', async () => {
    const user = userEvent.setup()
    api.importModelApis.mockReturnValue(new Promise(() => {}))
    renderDialog()
    await user.click(await screen.findByRole('button', { name: 'Import 1' }))
    const importing = await screen.findByRole('button', { name: 'Importing...' })
    expect(focused()).toBe(importing)

    await user.keyboard('{Escape}')

    await expectClosed()
  })

  it('moves focus to the reason an import failed, where Escape closes the dialog', async () => {
    const user = userEvent.setup()
    api.importModelApis.mockRejectedValue(new Error(failure))
    renderDialog()

    await user.click(await screen.findByRole('button', { name: 'Import 1' }))

    await waitFor(() => expect(focused()).toHaveTextContent(failure))
    expect(screen.getByRole('button', { name: 'Import 1' })).toBeEnabled()
    await user.keyboard('{Escape}')
    await expectClosed()
  })

  it('moves focus from a checkbox the administrator moved to, to the reason an import failed', async () => {
    const user = userEvent.setup()
    let refuse: (error: Error) => void = () => {}
    api.importModelApis.mockReturnValue(new Promise((_, reject) => { refuse = reject }))
    renderDialog()
    await user.click(await screen.findByRole('button', { name: 'Import 1' }))
    await screen.findByRole('button', { name: 'Importing...' })
    const echo = screen.getByRole('checkbox', { name: 'Import Echo' })
    await user.click(echo)
    expect(echo).toHaveFocus()

    refuse(new Error(failure))

    // The message bar doesn't announce itself, so the reason takes focus even from a field, to be read.
    await waitFor(() => expect(focused()).toHaveTextContent(failure))
    expect(echo).toBeChecked()
  })

  it('leaves focus where Fluent puts it when the dialog opens again, still showing the last failure', async () => {
    const user = userEvent.setup()
    api.importModelApis.mockRejectedValue(new Error(failure))
    renderDialog()
    await user.click(await screen.findByRole('button', { name: 'Import 1' }))
    await waitFor(() => expect(focused()).toHaveTextContent(failure))
    await user.keyboard('{Escape}')

    await user.click(await screen.findByRole('button', { name: 'Import from a gateway' }))

    // Fluent focuses the first control Tab reaches.
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Gateway to import from' })).toHaveFocus())
    expect(screen.getByText(failure)).toBeVisible()
  })

  it('keeps focus on Clear once it leaves nothing to clear', async () => {
    const user = userEvent.setup()
    renderDialog()
    await screen.findByRole('button', { name: 'Import 1' })
    const clear = screen.getByRole('button', { name: 'Clear' })

    await user.click(clear)

    // A browser takes focus off a button that becomes disabled, so Clear stays focusable instead.
    expect(await screen.findByRole('button', { name: 'Import 0' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Import Chat completions' })).not.toBeChecked()
    expect(clear).toHaveFocus()
    expect(clear).toHaveAttribute('aria-disabled', 'true')
    expect(clear).not.toBeDisabled()
    await user.click(clear)
    expect(screen.getByText('0 selected')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Select all' }))
    expect(await screen.findByRole('button', { name: 'Import 2' })).toBeEnabled()
    expect(clear).not.toHaveAttribute('aria-disabled')

    await user.click(clear)
    expect(clear).toHaveFocus()
    await user.keyboard('{Escape}')
    await expectClosed()
  })
})
