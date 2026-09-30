import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import type { EnvironmentCatalogView } from '../types'
import { ReviewEnvironmentSuggestionsDialog } from './ReviewEnvironmentSuggestionsDialog'

const api = {
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentSuggestions: vi.fn(),
  assignEnvironments: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

const catalog: EnvironmentCatalogView = {
  environments: [],
  requireClassification: false,
  unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
  compatibility: [],
  updatedAt: null,
}

function renderDialog() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ReviewEnvironmentSuggestionsDialog open onClose={vi.fn()} />
    </QueryClientProvider>,
  )
}

describe('ReviewEnvironmentSuggestionsDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue(catalog)
    api.listEnvironmentSuggestions.mockResolvedValue({
      items: [
        {
          resourceKind: 'gateway',
          resourceId: 'gateway_1',
          resourceName: 'Gateway',
          suggestedEnvironment: null,
          source: null,
          evidence: null,
        },
      ],
    })
  })

  it('names blocked MCP endpoint suggestions as MCP servers', async () => {
    api.assignEnvironments.mockRejectedValueOnce(
      new ApiError('Blocked', 409, {
        details: {
          reason: 'publicationsBlocked',
          publications: [],
          suggestedAssignments: [
            { resourceKind: 'mcpEndpoint', resourceId: 'mcp_endpoint_1', environment: 'production' },
            { resourceKind: 'mcpEndpoint', resourceId: 'mcp_endpoint_2', environment: 'production' },
          ],
        },
      }),
    )
    renderDialog()

    await userEvent.click(await screen.findByRole('checkbox', { name: 'Select Gateway' }))
    await userEvent.click(screen.getByRole('button', { name: 'Submit selections' }))

    expect(await screen.findByRole('button', { name: 'Also classify suggested MCP servers' })).toBeVisible()
  })
})
