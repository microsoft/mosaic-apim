import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { MosaicThemeProvider } from '../theme'
import { emailSettingsFixture } from '../test/budget-fixtures'
import type { EmailSettings, EnvironmentCatalogView } from '../types'
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
  getEmailSettings: vi.fn(),
  saveEmailSettings: vi.fn(),
  sendTestEmail: vi.fn(),
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
    api.getEmailSettings.mockResolvedValue(emailSettingsFixture)
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
    const heading = await screen.findByRole('heading', { name: 'Environments' })
    expect(heading).toBeVisible()
    expect(within(heading.parentElement!).getByText('Live data')).toBeVisible()
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

  it('saves email settings, offering the deployment’s own Communication Services', async () => {
    const user = userEvent.setup()
    const saved: EmailSettings = {
      ...emailSettingsFixture,
      enabled: true,
      endpoint: 'https://contoso-mosaic.communication.azure.com',
      sender: 'DoNotReply@contoso.azurecomm.net',
      ready: true,
      updatedAt: '2026-09-01T12:00:00Z',
    }
    api.saveEmailSettings.mockResolvedValue(saved)
    renderSettings()

    const form = await screen.findByRole('form', { name: 'Email settings' })
    expect(screen.getByText('Not set up')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Send test email' })).toBeDisabled()
    await user.click(within(form).getByRole('button', { name: 'Use them' }))
    expect(within(form).getByRole('textbox', { name: 'Communication Services endpoint' })).toHaveValue(
      'https://contoso-mosaic.communication.azure.com',
    )
    await user.click(within(form).getByRole('switch', { name: 'Send budget email' }))
    await user.click(within(form).getByRole('button', { name: 'Save email settings' }))

    await waitFor(() =>
      expect(api.saveEmailSettings).toHaveBeenCalledWith({
        enabled: true,
        endpoint: 'https://contoso-mosaic.communication.azure.com',
        sender: 'DoNotReply@contoso.azurecomm.net',
      }),
    )
    expect(await screen.findByText(/Budget email goes out through this Communication Services/)).toBeVisible()
    expect(screen.getByText('On')).toBeVisible()
    expect(within(form).queryByRole('button', { name: 'Use them' })).not.toBeInTheDocument()
  })

  it('refuses to turn email on without an endpoint and a sender', async () => {
    const user = userEvent.setup()
    renderSettings()
    const form = await screen.findByRole('form', { name: 'Email settings' })
    await user.click(within(form).getByRole('switch', { name: 'Send budget email' }))
    await user.click(within(form).getByRole('button', { name: 'Save email settings' }))
    expect(
      await screen.findByText(/Email can be on only with a Communication Services endpoint/),
    ).toBeVisible()
    expect(api.saveEmailSettings).not.toHaveBeenCalled()
  })

  it('sends a test email from the saved settings, and shows why one failed', async () => {
    const user = userEvent.setup()
    api.getEmailSettings.mockResolvedValue({
      ...emailSettingsFixture,
      endpoint: 'https://contoso-mosaic.communication.azure.com',
      sender: 'DoNotReply@contoso.azurecomm.net',
      lastTestAt: '2026-09-01T12:00:00Z',
      lastTestError: 'Communication Services refused the call (403)',
    })
    api.sendTestEmail
      .mockResolvedValueOnce({ sent: true, to: 'avery@contoso.com', operationId: 'op-1', error: null })
      .mockRejectedValueOnce(new ApiError('A test email went less than 30 seconds ago', 429))
    renderSettings()

    const test = await screen.findByRole('form', { name: 'Test email' })
    expect(screen.getByText('Off')).toBeVisible()
    expect(within(test).getByText(/failed: Communication Services refused the call \(403\)/)).toBeVisible()
    await user.type(within(test).getByRole('textbox', { name: 'Send to' }), 'avery@contoso.com')
    await user.click(within(test).getByRole('button', { name: 'Send test email' }))
    await waitFor(() => expect(api.sendTestEmail).toHaveBeenCalledWith('avery@contoso.com'))
    expect(await within(test).findByText(/accepted a test email to avery@contoso.com/)).toBeVisible()

    await user.click(within(test).getByRole('button', { name: 'Send test email' }))
    expect(await within(test).findByText('A test email went less than 30 seconds ago')).toBeVisible()
  })

  it('makes unsaved email changes wait before a test', async () => {
    const user = userEvent.setup()
    api.getEmailSettings.mockResolvedValue({
      ...emailSettingsFixture,
      endpoint: 'https://contoso-mosaic.communication.azure.com',
      sender: 'DoNotReply@contoso.azurecomm.net',
    })
    renderSettings()
    const form = await screen.findByRole('form', { name: 'Email settings' })
    const test = screen.getByRole('form', { name: 'Test email' })
    expect(within(test).getByRole('button', { name: 'Send test email' })).toBeEnabled()
    await user.type(within(form).getByRole('textbox', { name: 'Sender address' }), 'x')
    expect(within(test).getByRole('button', { name: 'Send test email' })).toBeDisabled()
    expect(within(test).getByText(/Save your changes first/)).toBeVisible()
  })
})
