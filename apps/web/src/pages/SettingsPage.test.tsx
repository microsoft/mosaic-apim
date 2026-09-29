import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { MosaicThemeProvider } from '../theme'
import type { EnvironmentCatalogView } from '../types'
import { SettingsPage } from './SettingsPage'

vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({ accounts: [] }),
}))

const api = {
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
  createEnvironment: vi.fn(),
  updateEnvironment: vi.fn(),
  deleteEnvironment: vi.fn(),
  updateEnvironmentSettings: vi.fn(),
  listEnvironmentSuggestions: vi.fn(),
  assignEnvironments: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

const catalog: EnvironmentCatalogView = {
  environments: [
    {
      key: 'development',
      displayName: 'Development',
      description: null,
      color: 'brand',
      production: false,
      aliases: ['dev'],
      acceptsEndpointsFrom: [],
      order: 10,
      builtIn: true,
      usage: { gateways: 1, modelEndpoints: 2, mcpEndpoints: 0 },
    },
    {
      key: 'production',
      displayName: 'Production',
      description: null,
      color: 'danger',
      production: true,
      aliases: ['prod'],
      acceptsEndpointsFrom: [],
      order: 20,
      builtIn: true,
      usage: { gateways: 3, modelEndpoints: 4, mcpEndpoints: 1 },
    },
    {
      key: 'custom',
      displayName: 'Custom',
      description: null,
      color: 'success',
      production: false,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 30,
      builtIn: false,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
  ],
  requireClassification: false,
  unclassified: { gateways: 1, modelEndpoints: 1, mcpEndpoints: 1 },
  compatibility: [],
  updatedAt: null,
}

function renderSettings() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MosaicThemeProvider>
        <SettingsPage />
      </MosaicThemeProvider>
    </QueryClientProvider>,
  )
}

describe('SettingsPage', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue(catalog)
    api.listEnvironmentFindings.mockResolvedValue({
      items: [],
      limitations: [],
      generatedAt: '2026-09-01T12:00:00Z',
    })
    api.listEnvironmentSuggestions.mockResolvedValue({ items: [] })
  })

  it('applies and persists appearance preferences', async () => {
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('radio', { name: 'Dark' }))

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(screen.getByText(/Resolved theme: dark/i)).toBeVisible()
  })

  it('renders the live environment catalog', async () => {
    renderSettings()
    expect(await screen.findByRole('heading', { name: 'Environments' })).toBeVisible()
    expect(screen.getByText('Live data')).toBeVisible()
    expect(await screen.findByText('Development')).toBeVisible()
    expect(
      screen.getByText(/3 resources need classification: 1 gateway, 1 model endpoint, 1 MCP server\./),
    ).toBeVisible()
    expect(screen.getByText('2 model endpoints')).toBeVisible()
    expect(screen.getByText('1 MCP server')).toBeVisible()
  })

  it('validates create keys before calling the API', async () => {
    const user = userEvent.setup()
    renderSettings()
    await user.click(await screen.findByRole('button', { name: 'Add environment' }))
    await user.type(screen.getByRole('textbox', { name: 'Key' }), '1bad')
    await user.type(screen.getByRole('textbox', { name: 'Display name' }), 'Bad')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/Use 2–32 lowercase/)).toBeVisible()
    expect(api.createEnvironment).not.toHaveBeenCalled()
  })

  it('disables non-production exceptions for production-class environments', async () => {
    const user = userEvent.setup()
    renderSettings()
    await screen.findByText('Production')
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[1])
    const exceptions = await screen.findByRole('group', { name: 'Exceptions' })
    const boxes = within(exceptions).getAllByRole('checkbox')
    expect(new Set(boxes.map((box) => box.id)).size).toBe(boxes.length)
    expect(within(exceptions).getByRole('checkbox', { name: 'Development' })).toBeDisabled()
    expect(exceptions).toHaveAccessibleDescription(
      /may list only other production-class environments/,
    )
  })

  it('shows delete refusals with usage details', async () => {
    const user = userEvent.setup()
    api.deleteEnvironment.mockRejectedValue(
      new ApiError('Environment in use', 409, {
        details: {
          reason: 'environmentInUse',
          environment: 'custom',
          usage: { gateways: 1, modelEndpoints: 0, mcpEndpoints: 0 },
          resources: [],
          referencedBy: ['production'],
        },
      }),
    )
    renderSettings()
    await screen.findAllByText('Custom')
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getAllByRole('button', { name: 'Delete' }).at(-1)!)
    expect(await screen.findByText(/Environment is in use/)).toBeVisible()
    expect(screen.getByText(/1 gateway, 0 model endpoints, and 0 MCP servers/)).toBeVisible()
  })

  it('shows require-classification publication refusals', async () => {
    const user = userEvent.setup()
    api.updateEnvironmentSettings.mockRejectedValue(
      new ApiError('Blocked', 409, {
        details: {
          reason: 'publicationsBlocked',
          publications: [
            {
              publicationId: 'pub_1',
              status: 'applied',
              gatewayId: 'gateway_1',
              gatewayName: 'Gateway',
              gatewayEnvironment: 'production',
              modelEndpointId: 'endpoint_1',
              modelEndpointName: 'Endpoint',
              endpointEnvironment: null,
              deploymentName: 'gpt-4o',
              verdict: {
                level: 'blocked',
                reason: 'Classify the endpoint first.',
                gatewayEnvironment: 'production',
                endpointEnvironment: null,
                viaException: false,
              },
            },
          ],
          suggestedAssignments: [],
        },
      }),
    )
    renderSettings()
    await user.click(await screen.findByRole('switch', { name: 'Require classification before publishing' }))
    expect(await screen.findByText(/Environment rules block this change/)).toBeVisible()
    expect(screen.getByText('Classify the endpoint first.')).toBeVisible()
  })

  it('reviews suggestions with one batch and shows per-row results', async () => {
    const user = userEvent.setup()
    api.listEnvironmentSuggestions.mockResolvedValue({
      items: [
        {
          resourceKind: 'gateway',
          resourceId: 'gateway_1',
          resourceName: 'Gateway',
          suggestedEnvironment: 'production',
          source: 'azureTag',
          evidence: 'Azure tag environment = "prod"',
        },
      ],
    })
    api.assignEnvironments.mockResolvedValue({
      results: [
        {
          resourceKind: 'gateway',
          resourceId: 'gateway_1',
          resourceName: 'Gateway',
          previousEnvironment: null,
          environment: 'production',
          status: 'applied',
          message: 'Applied',
        },
      ],
      grantsCarried: 0,
      warnings: [],
    })
    renderSettings()
    await user.click(await screen.findByRole('button', { name: 'Review suggestions' }))
    await screen.findByText('Azure tag environment = "prod"')
    await user.click(screen.getByRole('button', { name: 'Submit selections' }))
    await waitFor(() =>
      expect(api.assignEnvironments).toHaveBeenCalledWith({
        assignments: [
          { resourceKind: 'gateway', resourceId: 'gateway_1', environment: 'production' },
        ],
        acknowledgeGrants: false,
      }),
    )
    expect(await screen.findByText(/Gateway: applied/)).toBeVisible()
  })
})
