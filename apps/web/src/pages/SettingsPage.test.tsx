import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
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
  getDirectoryStatus: vi.fn(),
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
  createEnvironment: vi.fn(),
  updateEnvironment: vi.fn(),
  deleteEnvironment: vi.fn(),
  updateEnvironmentSettings: vi.fn(),
  listEnvironmentSuggestions: vi.fn(),
  assignEnvironments: vi.fn(),
  getEmailSettings: vi.fn(),
  checkEmailAccess: vi.fn(),
  saveEmailSettings: vi.fn(),
  sendTestEmail: vi.fn(),
  getNotificationSettings: vi.fn(),
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

function renderSettings(section = 'environments') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MosaicThemeProvider>
        <MemoryRouter initialEntries={[`/settings?tab=${section}`]}><SettingsPage /></MemoryRouter>
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
    api.getNotificationSettings.mockResolvedValue({ inAppEnabled: true, emailEnabled: false, teamsChannelEnabled: false, teamsPersonalEnabled: false, retentionDays: 90, revision: 1 })
    api.getDirectoryStatus.mockResolvedValue({
      lookupEnabled: true,
      groupClaimsEnabled: false,
      permissions: [
        { permission: 'User.ReadBasic.All', capability: 'People and agent users', required: true, granted: true },
        { permission: 'GroupMember.Read.All', capability: 'Entra security groups and members', required: true, granted: true },
        { permission: 'AgentIdentity.Read.All', capability: 'Entra agent identities', required: true, granted: false },
        { permission: 'Application.Read.All', capability: 'Applications and managed identities', required: false, granted: false },
        { permission: 'ProfilePhoto.Read.All', capability: 'People and administrator photos', required: false, granted: false },
      ],
    })
  })

  it('applies and persists appearance preferences', async () => {
    const user = userEvent.setup()
    renderSettings()

    expect(screen.queryByRole('radio', { name: 'Dark' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Appearance' }))
    await user.click(await screen.findByRole('radio', { name: 'Dark' }))

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(screen.getByText(/Resolved theme: dark/i)).toBeVisible()
  })

  it('renders the live environment catalog', async () => {
    renderSettings()
    const heading = await screen.findByRole('heading', { name: 'Environments' })
    expect(heading).toBeVisible()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
    expect(await screen.findByText('Development')).toBeVisible()
    expect(
      screen.getByText(/3 resources need classification: 1 gateway, 1 model endpoint, 1 MCP server\./),
    ).toBeVisible()
    expect(screen.getByText('2 model endpoints')).toBeVisible()
    expect(screen.getByText('1 MCP server')).toBeVisible()
  })

  it('keeps persisted, read-only and local settings in separate views', async () => {
    const user = userEvent.setup()
    renderSettings()
    await screen.findByRole('heading', { name: 'Environments' })
    expect(screen.queryByRole('form', { name: 'Email settings' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Runtime configuration' }))
    expect(screen.getByRole('heading', { name: 'Runtime identity and configuration' })).toBeVisible()
    expect(await screen.findByText(/1 required Graph permission is missing/)).toBeVisible()
    expect(screen.getByText(/Directory lookup is enabled with Entra authentication/)).toBeVisible()
    expect(screen.queryByText(/MOSAIC_ENTRA_DIRECTORY_LOOKUP/)).not.toBeInTheDocument()
    expect(screen.getAllByText('ProfilePhoto.Read.All')[0]).toBeVisible()
    expect(screen.getAllByText('Missing')).toHaveLength(3)
    expect(api.getDirectoryStatus).toHaveBeenCalledWith(true)
    expect(screen.queryByRole('textbox', { name: 'Support destination alias' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add environment' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Notifications' }))
    await user.click(screen.getByRole('tab', { name: /^Email$/ }))
    expect(await screen.findByRole('heading', { name: 'Microsoft Graph email' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Save local preview' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Integration preview' })).not.toBeInTheDocument()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Appearance' }))
    expect(screen.getByRole('radiogroup', { name: 'MOSAIC theme preference' })).toBeVisible()
    expect(screen.getByRole('radio', { name: 'System' })).toBeChecked()
    expect(screen.queryByRole('form', { name: 'Email settings' })).not.toBeInTheDocument()
    expect(api.saveEmailSettings).not.toHaveBeenCalled()
  })

  it('opens the real environment settings for a removed integration-preview link', async () => {
    renderSettings('integrations')

    expect(await screen.findByText('Development')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Environments' })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('button', { name: 'Integration preview' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Support destination alias' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Observability workspace tag' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Change template ID' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save local preview' })).not.toBeInTheDocument()
    expect(api.createEnvironment).not.toHaveBeenCalled()
    expect(api.saveEmailSettings).not.toHaveBeenCalled()
  })

  it('keeps real environment errors visible rather than rendering preview data', async () => {
    api.getEnvironmentCatalog.mockRejectedValue(new Error('Environment catalog unavailable'))
    renderSettings('integrations')

    expect(await screen.findByText('Environment catalog unavailable')).toBeVisible()
    expect(screen.queryByText('Development')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Integration preview' })).not.toBeInTheDocument()
  })

  it('explains local-authentication directory limitations without an enable switch', async () => {
    api.getDirectoryStatus.mockResolvedValue({
      lookupEnabled: false,
      groupClaimsEnabled: true,
      permissions: [
        { permission: 'User.ReadBasic.All', capability: 'People and agent users', required: true, granted: null },
      ],
    })
    renderSettings('runtime')
    expect(await screen.findByText(/Directory lookup requires Entra authentication/)).toBeVisible()
    expect(screen.getByText(/Use manual entry to record identities without directory verification/)).toBeVisible()
    expect(screen.getByText('User.ReadBasic.All')).toBeVisible()
    expect(screen.queryByText(/MOSAIC_ENTRA_DIRECTORY_LOOKUP/)).not.toBeInTheDocument()
  })

  it('keeps directory status failures distinct from local-authentication limitations', async () => {
    const user = userEvent.setup()
    api.getDirectoryStatus.mockRejectedValue(new Error('Directory status could not be read'))
    renderSettings('runtime')
    expect(await screen.findByText('Directory configuration unavailable')).toBeVisible()
    expect(screen.getByText('Directory status could not be read')).toBeVisible()
    expect(screen.queryByText(/Directory lookup requires Entra authentication/)).not.toBeInTheDocument()
    api.getDirectoryStatus.mockResolvedValue({
      lookupEnabled: true,
      groupClaimsEnabled: true,
      permissions: [
        { permission: 'User.ReadBasic.All', capability: 'People and agent users', required: true, granted: true },
      ],
    })
    await user.click(screen.getByRole('button', { name: 'Refresh permission status' }))
    expect(await screen.findByText(/Directory lookup is enabled with Entra authentication/)).toBeVisible()
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

  it('redirects legacy email settings and saves a scoped Graph mailbox', async () => {
    const user = userEvent.setup()
    const saved: EmailSettings = {
      ...emailSettingsFixture,
      enabled: true,
      senderMailboxId: '33333333-3333-4333-8333-333333333333',
      sender: 'notifications@example.test',
      configured: true,
      revision: 2,
      ready: true,
      updatedAt: '2026-09-01T12:00:00Z',
    }
    api.saveEmailSettings.mockResolvedValue(saved)
    renderSettings('email')

    await screen.findByRole('heading', { name: 'Microsoft Graph email' })
    expect(await screen.findByText('Not configured')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Send test email' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Configure sender' }))
    await user.type(screen.getByRole('textbox', { name: 'Sender mailbox object ID' }), saved.senderMailboxId!)
    await user.type(screen.getByRole('textbox', { name: 'Sender address label (optional)' }), saved.sender!)
    await user.click(screen.getByRole('checkbox', { name: 'Send budget email' }))
    await user.click(screen.getByRole('button', { name: 'Save email settings' }))

    await waitFor(() =>
      expect(api.saveEmailSettings).toHaveBeenCalledWith({
        enabled: true,
        senderMailboxId: saved.senderMailboxId,
        sender: saved.sender,
        expectedRevision: 1,
      }),
    )
    expect(await screen.findByText('Sender saved')).toBeVisible()
    expect(screen.getByText(saved.senderMailboxId!)).toBeVisible()
    expect(api.sendTestEmail).not.toHaveBeenCalled()
  })

  it('cannot enable budget mail without a valid mailbox object ID', async () => {
    const user = userEvent.setup()
    renderSettings('email')
    await user.click(await screen.findByRole('button', { name: 'Configure sender' }))
    await user.click(screen.getByRole('checkbox', { name: 'Send budget email' }))
    expect(screen.getByRole('button', { name: 'Save email settings' })).toBeDisabled()
    await user.type(screen.getByRole('textbox', { name: 'Sender mailbox object ID' }), 'sender@example.test')
    expect(screen.getByRole('button', { name: 'Save email settings' })).toBeDisabled()
    expect(screen.getByText(/Enter the mailbox user object ID as a GUID/)).toBeVisible()
    expect(api.saveEmailSettings).not.toHaveBeenCalled()
  })

  it('sends a test email from the saved settings, and shows why one failed', async () => {
    const user = userEvent.setup()
    api.getEmailSettings.mockResolvedValue({
      ...emailSettingsFixture,
      senderMailboxId: '33333333-3333-4333-8333-333333333333',
      configured: true,
      sender: 'notifications@example.test',
      lastTestAt: '2026-09-01T12:00:00Z',
      lastTestRevision: 1,
      lastTestOutcome: 'permanentFailure',
      lastTestError: 'Graph denied this sender. Check mailbox-scoped Application Mail.Send.',
    })
    api.sendTestEmail
      .mockResolvedValueOnce({ sent: true, to: 'avery@contoso.com', operationId: 'op-1', error: null, outcome: 'providerAccepted', testedAt: '2026-09-01T12:00:00Z', configurationRevision: 1 })
      .mockRejectedValueOnce(new ApiError('A test email went less than 30 seconds ago', 429))
    renderSettings('email')

    const test = await screen.findByRole('region', { name: 'Test email' })
    expect(await within(test).findByText(/Graph denied this sender/)).toBeVisible()
    await user.type(within(test).getByRole('textbox', { name: 'Test recipient' }), 'avery@contoso.com')
    await user.click(await within(test).findByRole('button', { name: 'Send test email' }))
    expect(api.sendTestEmail).not.toHaveBeenCalled()
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Send synthetic email' }))
    await waitFor(() => expect(api.sendTestEmail).toHaveBeenCalledWith('avery@contoso.com'))
    expect(await screen.findByText(/Graph accepted the test request. Check the recipient mailbox/)).toBeVisible()

    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
    await user.click(await within(test).findByRole('button', { name: 'Send test email' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Send synthetic email' }))
    expect(await screen.findByText('A test email went less than 30 seconds ago')).toBeVisible()
  })

  it('keeps read-only access checks separate from sends and explains failures', async () => {
    const user = userEvent.setup()
    api.checkEmailAccess.mockResolvedValue([{
      feature: 'emailIdentity', scope: 'tenant', required: true, state: 'failed',
      reason: 'The API managed identity could not acquire a token.', evidence: [],
      checkedAt: '2026-09-01T12:00:00Z',
      remediation: { action: 'Attach the correct managed identity to the API host.', owner: 'azureAdmin' },
    }])
    renderSettings('email')
    await user.click(await screen.findByRole('button', { name: 'Check email access' }))
    expect(await screen.findByText('The API managed identity could not acquire a token.')).toBeVisible()
    expect(screen.getByText('Attach the correct managed identity to the API host.')).toBeVisible()
    expect(api.sendTestEmail).not.toHaveBeenCalled()
    expect(api.saveEmailSettings).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Configure sender' }))
    expect(screen.queryByRole('button', { name: 'Send test email' })).not.toBeInTheDocument()
  })
})
