import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import type { EnvironmentAssignmentResult, EnvironmentCatalogView } from '../types'
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

  it('shows a failed suggestions query rather than an empty success', async () => {
    api.listEnvironmentSuggestions.mockRejectedValue(new Error('Suggestions could not be loaded. Retry later.'))
    renderDialog()
    expect(await screen.findByText('Suggestions could not be loaded. Retry later.')).toBeVisible()
    expect(screen.queryByText('No unclassified resources need review.')).not.toBeInTheDocument()
  })

  it('keeps pending focus, blocks duplicate submits, and focuses the result when no rows remain', async () => {
    const user = userEvent.setup()
    let finish!: (result: EnvironmentAssignmentResult) => void
    api.assignEnvironments.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    renderDialog()
    await user.click(await screen.findByRole('checkbox', { name: 'Select Gateway' }))
    const submit = screen.getByRole('button', { name: 'Submit selections' })
    await user.click(submit)
    expect(submit).toHaveFocus()
    expect(submit).toHaveAttribute('aria-disabled', 'true')
    expect(submit).not.toBeDisabled()
    await user.click(submit)
    expect(api.assignEnvironments).toHaveBeenCalledOnce()
    api.listEnvironmentSuggestions.mockResolvedValue({ items: [] })
    await act(async () => finish({
      results: [{
        resourceKind: 'gateway', resourceId: 'gateway_1', resourceName: 'Gateway',
        environment: 'production', previousEnvironment: null, status: 'applied',
      }],
      grantsCarried: 0, warnings: [],
    }))
    expect(await screen.findByText('No unclassified resources need review.')).toBeVisible()
    await waitFor(() => expect(screen.getByText('Gateway: applied').closest('[tabindex]')).toHaveFocus())
    expect(submit).toBeDisabled()
  })
})
