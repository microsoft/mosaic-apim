import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type {
  Entitlement,
  PortalBudgetAlert,
  PortalProfile,
  ResolvedEntitlement,
  ResourceSummary,
} from '../types'
import { MyAccessPage } from './MyAccessPage'
import { connection, directResolved, revealedPrimary } from '../test/connection'

const mocks = vi.hoisted(() => ({
  api: {} as PortalApi,
}))

vi.mock('@azure/msal-react', () => ({ useMsal: () => ({ accounts: [] }) }))

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {
    status = 500
  },
  usePortalApi: () => mocks.api,
}))

const baseEntitlement: Entitlement = {
  id: 'entitlement-1',
  tenantId: 'tenant-1',
  entityType: 'entitlement',
  subject: { kind: 'group', id: 'group-1' },
  resource: { kind: 'modelApi', id: 'chat-completions', scopeId: 'gateway-1' },
  enabled: true,
  enforcement: {
    tokens: {
      counterKeyExpression: '@(context.Subscription?.Key)',
      tokensPerMinute: 10_000,
      tokenQuota: null,
      tokenQuotaPeriod: null,
      estimatePromptTokens: true,
    },
    requests: {
      counterKeyExpression: '@(context.Subscription?.Key)',
      calls: 600,
      renewalPeriodSeconds: 60,
      callQuota: 100_000,
      callQuotaPeriod: 'Monthly',
    },
  },
  binding: null,
  notes: null,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
}

const profile: PortalProfile = {
  objectId: 'user-object-1',
  tenantId: 'tenant-1',
  roles: ['User'],
  isAdmin: false,
  principalId: 'principal-1',
  displayLabel: 'Ada Lovelace',
  entitlementCount: 0,
  pendingRequestCount: 0,
  groupsOverage: false,
}

const baseSummary: ResourceSummary = {
  kind: 'modelApi',
  id: 'chat-completions',
  scopeId: 'gateway-1',
  displayName: 'Chat completions',
  gatewayId: 'gateway-1',
  gatewayName: 'Production gateway',
  environment: 'production',
  available: true,
}

const research = { id: 'cc-research', name: 'Research', code: 'RES' }
const finance = { id: 'cc-finance', name: 'Finance', code: 'FIN' }

function HistoryControls() {
  const navigate = useNavigate()
  const location = useLocation()
  return <>
    <button onClick={() => void navigate(-1)}>History back</button>
    <button onClick={() => void navigate(1)}>History forward</button>
    <span data-testid="route" data-search={location.search} />
  </>
}

function renderPage(
  entitlements: ResolvedEntitlement[],
  profileOverride: Partial<PortalProfile> = {},
  budgets: PortalBudgetAlert[] = [],
  initialRoute?: string,
) {
  const api = {
    getProfile: async () => ({ ...profile, ...profileOverride }),
    listEntitlements: async () => entitlements,
    listEnvironments: async () => [
      { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, order: 50 },
    ],
    getMyBudgets: async () => budgets,
    getMyEntitlementConnection: vi.fn(),
    getMcpConnection: vi.fn(),
    revealMyEntitlementKey: vi.fn(),
  }
  mocks.api = api as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(
    <FluentProvider theme={webLightTheme}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialRoute ?? `/access${entitlements.length === 1 ? `?grant=${encodeURIComponent(entitlements[0].entitlement.id)}` : ''}`]}>
          {initialRoute && <HistoryControls />}
          <MyAccessPage />
        </MemoryRouter>
      </QueryClientProvider>
    </FluentProvider>,
  )
  return { ...api, ...rendered }
}

describe('MyAccessPage', () => {
  it('says when a cost center is blocked or near its budget, with totals only', async () => {
    renderPage(
      [{ entitlement: baseEntitlement, resourceSummary: baseSummary, via: 'direct', viaGroupId: null, viaGroupName: null }],
      {},
      [
        { costCenter: research, level: 'blocked', month: '2026-03', used: 1.02, action: 'block' },
        { costCenter: finance, level: 'warning', month: '2026-03', used: 0.876, action: 'block' },
      ],
    )

    const banners = await screen.findByRole('region', { name: 'Cost center budgets' })
    expect(within(banners).getByText('Research has used its monthly budget')).toBeVisible()
    // Its share, and only what its own budget does: nothing about other cost centers' calls.
    expect(
      within(banners).getByText(
        /It has used 102% of this month’s budget, so calls charged to RES are refused until the budget is raised or a new month starts/,
      ),
    ).toBeVisible()
    expect(banners).not.toHaveTextContent(/other cost centers/)
    expect(within(banners).getByText('Finance is near its monthly budget')).toBeVisible()
    expect(within(banners).getByText(/It has used 87% of this month’s budget\. At 100%, calls charged to FIN/)).toBeVisible()
    expect(within(banners).getByText(/each cost center’s total, from everyone who charges it/)).toBeVisible()
  })

  it('says a cost center is over budget when its calls continue', async () => {
    renderPage([], {}, [
      { costCenter: finance, level: 'exceeded', month: '2026-03', used: 1.124, action: 'continue' },
    ])

    expect(await screen.findByText('Finance is over its monthly budget')).toBeVisible()
    expect(
      screen.getByText(/It has used 112% of this month’s budget\. This budget only warns, so it doesn’t stop calls charged to FIN\./),
    ).toBeVisible()
  })

  it('says what happens at 100% when a budget only warns', async () => {
    renderPage([], {}, [
      { costCenter: finance, level: 'warning', month: '2026-03', used: 0.81, action: 'continue' },
    ])

    expect(await screen.findByText('Finance is near its monthly budget')).toBeVisible()
    expect(
      screen.getByText(/It has used 81% of this month’s budget\. At 100%, this budget only warns, so it doesn’t stop calls charged to FIN\./),
    ).toBeVisible()
  })

  it('shows no budget banner while every budget is on track', async () => {
    renderPage([{ entitlement: baseEntitlement, resourceSummary: baseSummary, via: 'direct', viaGroupId: null, viaGroupName: null }])

    expect(await screen.findByText('Chat completions')).toBeVisible()
    expect(screen.queryByRole('region', { name: 'Cost center budgets' })).not.toBeInTheDocument()
  })

  it('renders group attribution for entitlements', async () => {
    renderPage([
      {
        entitlement: baseEntitlement,
        resourceSummary: baseSummary,
        via: 'group',
        viaGroupId: 'group-1',
        viaGroupName: 'Platform engineering',
      },
    ])

    expect(await screen.findByText('Chat completions')).toBeVisible()
    expect(screen.getByText('Model API · Granted through Platform engineering')).toBeVisible()
    expect(within(screen.getByRole('dialog')).getByText('Production')).toBeVisible()
    expect(screen.getByText('Production gateway')).toBeVisible()
    expect(screen.getByText('10,000 tokens per minute')).toBeVisible()
    expect(screen.getByText('600 calls per 60 seconds')).toBeVisible()
    expect(screen.getByText('100,000 calls per month')).toBeVisible()
    expect(screen.getByText("Usage can't be measured for this grant yet.")).toBeVisible()
    expect(within(screen.getByRole('dialog')).getByText('Recorded grant')).toBeVisible()
  })

  it('groups a resource once and keeps its cost-center grants separate in the drawer', async () => {
    renderPage([
      {
        entitlement: { ...baseEntitlement, id: 'finance-grant', costCenterId: finance.id },
        costCenter: finance,
        resourceSummary: baseSummary,
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
      {
        entitlement: { ...baseEntitlement, id: 'research-grant', costCenterId: research.id },
        costCenter: research,
        resourceSummary: baseSummary,
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
    ])

    expect(await screen.findAllByRole('heading', { level: 2, name: 'Chat completions' })).toHaveLength(1)
    await userEvent.click(screen.getByRole('button', { name: 'View access for Chat completions' }))
    const selector = screen.getByRole('combobox', { name: 'Grant' })
    expect(selector).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Connection details' })).not.toBeInTheDocument()
    await userEvent.selectOptions(selector, 'finance-grant')
    expect(within(screen.getByRole('dialog')).getByText('Finance · FIN')).toBeVisible()
    await userEvent.selectOptions(selector, 'research-grant')
    expect(within(screen.getByRole('dialog')).getByText('Research · RES')).toBeVisible()
  })

  it('groups known models across resources while retaining searchable environment and cost-center context', async () => {
    renderPage([
      {
        ...directResolved,
        model: { name: 'gpt-4o', format: 'OpenAI', version: null },
        resourceSummary: { ...baseSummary, displayName: 'Production chat' },
        costCenter: research,
      },
      {
        ...directResolved,
        entitlement: { ...directResolved.entitlement, id: 'development-grant', resource: { kind: 'modelApi', id: 'development-api', scopeId: null } },
        model: { name: 'gpt-4o', format: 'OpenAI', version: null },
        resourceSummary: { ...baseSummary, id: 'development-api', displayName: 'Development chat', environment: 'development' },
        costCenter: finance,
      },
    ])
    expect(await screen.findByText('1 resource · 2 grants')).toBeVisible()
    const list = screen.getByRole('list')
    expect(within(list).getAllByRole('heading', { level: 2 })).toHaveLength(1)
    await userEvent.type(screen.getByRole('textbox', { name: 'Search my access' }), 'development chat')
    expect(screen.getByText('1 resource · 1 grant')).toBeVisible()
    expect(within(list).getByText('Finance · FIN', { exact: false })).toBeVisible()
    await userEvent.clear(screen.getByRole('textbox', { name: 'Search my access' }))
    await userEvent.selectOptions(screen.getByLabelText('Environment'), 'production')
    expect(screen.getByText('1 resource · 1 grant')).toBeVisible()
    await userEvent.selectOptions(screen.getByLabelText('Cost center'), finance.id)
    expect(screen.getByText('No access matches these filters')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(screen.getByText('1 resource · 2 grants')).toBeVisible()
  })

  it('keeps an exact grant deep link and browser history, without dropping unrelated parameters', async () => {
    const rows: ResolvedEntitlement[] = [
      { ...directResolved, entitlement: { ...directResolved.entitlement, id: 'research-grant' }, costCenter: research },
      { ...directResolved, entitlement: { ...directResolved.entitlement, id: 'finance-grant' }, costCenter: finance },
    ]
    renderPage(rows, {}, [], '/access?grant=research-grant&view=all')
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('combobox', { name: 'Grant' })).toHaveValue('research-grant')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Grant' }), 'finance-grant')
    expect(screen.getByTestId('route')).toHaveAttribute('data-search', '?grant=finance-grant&view=all')
    fireEvent.click(screen.getByText('History back'))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Grant' })).toHaveValue('research-grant'))
    fireEvent.click(screen.getByText('History forward'))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Grant' })).toHaveValue('finance-grant'))
    await userEvent.click(screen.getByRole('button', { name: 'Close details' }))
    expect(screen.getByTestId('route')).toHaveAttribute('data-search', '?view=all')
  })

  it('reports an invalid grant link and dismisses only the selection', async () => {
    renderPage([directResolved], {}, [], '/access?grant=missing&view=all')
    expect(await screen.findByText('This grant is not in your current access.')).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.getByTestId('route')).toHaveAttribute('data-search', '?view=all')
  })

  it('unmounts revealed credentials when switching and closing grants and never fetches before expansion', async () => {
    const rows: ResolvedEntitlement[] = [
      { ...directResolved, costCenter: research },
      { ...directResolved, entitlement: { ...directResolved.entitlement, id: 'other-grant' }, costCenter: finance },
    ]
    const api = renderPage(rows)
    api.getMyEntitlementConnection.mockResolvedValue(connection)
    api.revealMyEntitlementKey.mockResolvedValue(revealedPrimary)
    const opener = await screen.findByRole('button', { name: 'View access for Chat model API' })
    await userEvent.click(opener)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Grant' }), directResolved.entitlement.id)
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Connection details' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Show primary key' }))
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Grant' }), 'other-grant')
    expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Connection details' })).toHaveAttribute('aria-expanded', 'false')
    expect(api.getMyEntitlementConnection).toHaveBeenCalledOnce()
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Grant' }), directResolved.entitlement.id)
    await userEvent.click(screen.getByRole('button', { name: 'Connection details' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Show primary key' }))
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Close details' }))
    expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()
    await userEvent.click(opener)
    expect(screen.getByRole('combobox', { name: 'Grant' })).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Connection details' })).not.toBeInTheDocument()
    expect(api.revealMyEntitlementKey).toHaveBeenCalledTimes(2)
  })

  it('renders security group attribution and per-person limit guidance', async () => {
    renderPage([
      {
        entitlement: {
          ...baseEntitlement,
          subject: { kind: 'securityGroup', id: 'principal-group-1' },
        },
        resourceSummary: baseSummary,
        via: 'securityGroup',
        viaGroupId: 'principal-group-1',
        viaGroupName: 'AI builders',
      },
    ])

    expect(await screen.findByText('Chat completions')).toBeVisible()
    expect(screen.getByText('Model API · Granted through AI builders')).toBeVisible()
    expect(screen.getByText(/Group access limits apply to each person individually\./)).toBeVisible()
  })

  it('names an unnamed security group generically, never by its ID', async () => {
    renderPage([
      {
        entitlement: {
          ...baseEntitlement,
          subject: { kind: 'securityGroup', id: 'principal-group-1' },
        },
        resourceSummary: null,
        via: 'securityGroup',
        viaGroupId: null,
        viaGroupName: null,
      },
    ])

    expect(await screen.findByText('Granted through an assigned group')).toBeVisible()
    expect(screen.queryByText(/principal-group-1/)).not.toBeInTheDocument()
  })

  it('does not render defensive shadowed rows', async () => {
    renderPage([
      {
        entitlement: baseEntitlement,
        resourceSummary: baseSummary,
        via: 'group',
        viaGroupId: 'group-1',
        viaGroupName: 'Platform engineering',
        effective: false,
        shadowedBy: 'entitlement-direct',
      },
    ])

    expect(await screen.findByText('No access granted yet')).toBeVisible()
    expect(screen.queryByText('Chat completions')).not.toBeInTheDocument()
  })

  it('shows the groups-overage warning from the profile', async () => {
    renderPage([], { groupsOverage: true })

    expect(await screen.findByText('Group access may be missing')).toBeVisible()
    expect(screen.getByText(/too many groups for your sign-in token/)).toBeVisible()
  })

  it('hides the groups-overage warning when the profile is not overage', async () => {
    renderPage([], { groupsOverage: false })

    expect(await screen.findByText('No access granted yet')).toBeVisible()
    expect(screen.queryByText('Group access may be missing')).not.toBeInTheDocument()
  })

  it('distinguishes absent grant limits from inherited publication limits', async () => {
    renderPage([
      {
        entitlement: { ...baseEntitlement, enforcement: null },
        resourceSummary: baseSummary,
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
    ])

    expect(await screen.findByText('No additional grant limits configured')).toBeVisible()
    expect(screen.getByText(/Pending changes are not yet enforced by the gateway/)).toBeVisible()
    expect(screen.queryByText(/0/)).not.toBeInTheDocument()
  })

  it.each([
    ['pending', 'APIM changes pending'],
    ['applying', 'Applying to APIM'],
    ['applied', 'Applied to APIM'],
    ['revocationPending', 'APIM revocation pending'],
    ['revoked', 'Runtime access revoked'],
    ['failed', 'APIM apply failed'],
    ['unknown', 'Runtime status unknown'],
  ] as const)('renders %s runtime state separately from saved intent', async (status, label) => {
    renderPage([{
      entitlement: {
        ...baseEntitlement,
        subject: { kind: 'user', id: 'user-1' },
        binding: {
          gatewayId: 'gateway-1',
          apimProductName: 'model-product',
          apimSubscriptionName: 'grant-subscription',
          source: 'orchestrated',
        },
        runtime: {
          publicationId: 'publication-1',
          status,
          appliedMethods: { keysEnabled: true, entraEnabled: true },
          subscriptionName: 'grant-subscription',
          appliedAt: null,
          error: null,
        },
      },
      resourceSummary: baseSummary,
      via: 'direct',
      viaGroupId: null,
      viaGroupName: null,
    }])
    expect(await within(await screen.findByRole('dialog')).findByText(label)).toBeVisible()
    expect(
      screen.getByText('Linked through APIM subscription grant-subscription in product model-product.'),
    ).toBeVisible()
    expect(screen.queryByText('Enabled')).not.toBeInTheDocument()
  })

  it('describes gateway and fallback usage attribution in plain language', async () => {
    renderPage([
      {
        entitlement: {
          ...baseEntitlement,
          id: 'per-person',
          resource: { ...baseEntitlement.resource, id: 'team' },
          subject: { kind: 'securityGroup', id: 'security-group-1' },
          binding: {
            gatewayId: 'gateway-1',
            apimProductName: null,
            apimSubscriptionName: null,
            attributionKey: 'grant:per-person',
            attributionPerMember: true,
            source: 'orchestrated',
          },
        },
        resourceSummary: { ...baseSummary, displayName: 'Team chat' },
        via: 'securityGroup',
        viaGroupId: 'security-group-1',
        viaGroupName: 'AI builders',
      },
      {
        entitlement: {
          ...baseEntitlement,
          id: 'gateway-grant',
          resource: { ...baseEntitlement.resource, id: 'gateway' },
          binding: {
            gatewayId: 'gateway-1',
            apimProductName: null,
            apimSubscriptionName: null,
            attributionKey: 'grant:gateway',
            attributionPerMember: false,
            source: 'orchestrated',
          },
        },
        resourceSummary: { ...baseSummary, displayName: 'Gateway chat' },
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
      {
        entitlement: {
          ...baseEntitlement,
          id: 'subscription-grant',
          resource: { ...baseEntitlement.resource, id: 'subscription' },
          binding: {
            gatewayId: 'gateway-1',
            apimProductName: 'model-product',
            apimSubscriptionName: 'grant-subscription',
            source: 'orchestrated',
          },
        },
        resourceSummary: { ...baseSummary, displayName: 'Subscription chat' },
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
      {
        entitlement: {
          ...baseEntitlement,
          id: 'gateway-only',
          resource: { ...baseEntitlement.resource, id: 'pending' },
          binding: {
            gatewayId: 'gateway-1',
            apimProductName: null,
            apimSubscriptionName: null,
            source: 'orchestrated',
          },
        },
        resourceSummary: { ...baseSummary, displayName: 'Pending chat' },
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
    ])

    expect(await screen.findByText('Team chat')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'View access for Team chat' }))
    expect(screen.getByText('The gateway records each call you make with this grant, so your own usage can be measured.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
    fireEvent.click(screen.getByRole('button', { name: 'View access for Gateway chat' }))
    expect(screen.getByText('The gateway records each call made with this grant, so its usage can be measured.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
    fireEvent.click(screen.getByRole('button', { name: 'View access for Subscription chat' }))
    expect(
      screen.getByText('Linked through APIM subscription grant-subscription in product model-product.'),
    ).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
    fireEvent.click(screen.getByRole('button', { name: 'View access for Pending chat' }))
    expect(screen.getByText("Usage can't be measured for this grant yet.")).toBeVisible()
    expect(screen.queryByText('Gateway attribution configured')).not.toBeInTheDocument()
  })

  it('offers collapsed connection details for model API and MCP grants', async () => {
    const directUser = { kind: 'user' as const, id: 'user-1' }
    const api = renderPage([
      {
        entitlement: { ...baseEntitlement, id: 'model-grant', subject: directUser },
        resourceSummary: baseSummary,
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
      {
        entitlement: {
          ...baseEntitlement,
          id: 'mcp-grant',
          subject: directUser,
          resource: { kind: 'mcpServer', id: 'docs-mcp', scopeId: 'gateway-1' },
        },
        resourceSummary: {
          ...baseSummary,
          kind: 'mcpServer',
          id: 'docs-mcp',
          displayName: 'Docs MCP',
        },
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
    ])

    expect(await screen.findByText('Chat completions')).toBeVisible()
    expect(screen.getByText('Docs MCP')).toBeVisible()
    expect(screen.getByText('Recorded, not enforced by MOSAIC')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Connection details' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'View access for Chat completions' }))
    expect(screen.getByRole('button', { name: 'Connection details' })).toHaveAttribute('aria-expanded', 'false')
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
    expect(api.getMcpConnection).not.toHaveBeenCalled()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
  })

  it('shows a grant on a model served behind one API as a model, with connection details', async () => {
    const api = renderPage([
      {
        entitlement: {
          ...baseEntitlement,
          id: 'served-model-grant',
          subject: { kind: 'user', id: 'user-1' },
          resource: { kind: 'poolModel', id: 'pool_model_opus', scopeId: 'pool_anthropic' },
        },
        resourceSummary: {
          ...baseSummary,
          kind: 'poolModel',
          id: 'pool_model_opus',
          scopeId: 'pool_anthropic',
          displayName: 'Claude Opus',
        },
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
        resourceDisplayName: 'Claude Opus',
      },
    ])

    expect(await screen.findByRole('heading', { level: 2, name: 'Claude Opus' })).toBeVisible()
    expect(screen.getByText('Model · Granted directly to you')).toBeVisible()
    expect(screen.getByText(/^Model and gateway limits may also apply\./)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Connection details' })).toHaveAttribute('aria-expanded', 'false')
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
    expect(document.body).not.toHaveTextContent(/\bpools?\b|pool_anthropic|pool_model_opus/i)
  })

  it.each([
    ['pending', 'APIM changes pending'],
    ['applying', 'Applying to APIM'],
    ['applied', 'Applied to APIM'],
    ['revocationPending', 'APIM revocation pending'],
    ['revoked', 'Runtime access revoked'],
    ['failed', 'APIM apply failed'],
    ['unknown', 'Runtime status unknown'],
  ] as const)('renders MCP %s runtime state when the backend decorates it', async (status, label) => {
    renderPage([{
      entitlement: {
        ...baseEntitlement,
        subject: { kind: 'user', id: 'user-1' },
        resource: { kind: 'mcpServer', id: 'docs-mcp', scopeId: 'gateway-1' },
        runtime: {
          publicationId: 'mcp-publication-1',
          status,
          appliedMethods: { keysEnabled: false, entraEnabled: true },
          subscriptionName: null,
          appliedAt: null,
          error: null,
        },
      },
      resourceSummary: null,
      via: 'direct',
      viaGroupId: null,
      viaGroupName: null,
    }])

    expect(await within(await screen.findByRole('dialog')).findByText(label)).toBeVisible()
  })

  it('uses live summaries for removed resources without rendering raw resource IDs', async () => {
    renderPage([{
      entitlement: {
        ...baseEntitlement,
        resource: { kind: 'modelApi', id: 'modelApi_removed_123', scopeId: 'gateway-1' },
      },
      resourceSummary: {
        ...baseSummary,
        id: 'modelApi_removed_123',
        displayName: 'Retired chat',
        available: false,
      },
      via: 'direct',
      viaGroupId: null,
      viaGroupName: null,
      resourceDisplayName: null,
    }])

    const card = (await screen.findByText('Retired chat')).closest('.resource-row')
    expect(card).not.toBeNull()
    expect(within(card as HTMLElement).getByText('No longer available')).toBeVisible()
    expect(within(screen.getByRole('dialog')).getByText('Model API · Granted directly to you')).toBeVisible()
    expect(screen.queryByText(/modelApi_removed_123/)).not.toBeInTheDocument()
  })

  it('heads each grant with the name the catalog shows and keeps its kind visible', async () => {
    renderPage([
      {
        entitlement: baseEntitlement,
        resourceSummary: { ...baseSummary, displayName: 'Chat model' },
        via: 'group',
        viaGroupId: 'group-1',
        viaGroupName: 'Platform engineering',
        resourceDisplayName: 'Chat model',
      },
      {
        entitlement: {
          ...baseEntitlement,
          id: 'mcp-grant',
          subject: { kind: 'user', id: 'user-1' },
          resource: { kind: 'mcpServer', id: 'docs-mcp', scopeId: 'gateway-1' },
        },
        resourceSummary: {
          ...baseSummary,
          kind: 'mcpServer',
          id: 'docs-mcp',
          displayName: 'Docs search',
        },
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
        resourceDisplayName: 'Docs search',
      },
    ])

    expect(await screen.findByRole('heading', { level: 2, name: 'Chat model' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'View access for Chat model' }))
    expect(screen.getByText('Model API · Granted through Platform engineering')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
    expect(screen.getByRole('heading', { level: 2, name: 'Docs search' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'View access for Docs search' }))
    expect(screen.getByText('MCP server · Granted directly to you')).toBeVisible()
    expect(screen.queryByText(/chat-completions|docs-mcp/)).not.toBeInTheDocument()
  })

  it.each([
    ['an older API omits the name', {}],
    ['the API cannot resolve the name', { resourceDisplayName: null }],
    ['the API sends a blank name', { resourceDisplayName: '  ' }],
  ] satisfies [string, Partial<ResolvedEntitlement>][])(
    'falls back to the kind, never the ID, when %s',
    async (_, name) => {
      renderPage([
        {
          entitlement: baseEntitlement,
          resourceSummary: null,
          via: 'group',
          viaGroupId: 'group-1',
          viaGroupName: 'Platform engineering',
          ...name,
        },
      ])

      expect(
        await screen.findByRole('heading', { level: 2, name: 'Model API resource' }),
      ).toBeVisible()
      expect(screen.getByText('Granted through Platform engineering')).toBeVisible()
      expect(screen.queryByText(/chat-completions/)).not.toBeInTheDocument()
    },
  )
})
