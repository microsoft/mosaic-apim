import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
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

function renderPage(
  entitlements: ResolvedEntitlement[],
  profileOverride: Partial<PortalProfile> = {},
  budgets: PortalBudgetAlert[] = [],
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
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/access']}>
        <MyAccessPage />
      </MemoryRouter>
    </QueryClientProvider>,
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
    expect(
      within(banners).getByText(/Calls charged to RES are refused until its budget is raised or a new month starts/),
    ).toBeVisible()
    expect(within(banners).getByText('Finance is near its monthly budget')).toBeVisible()
    expect(within(banners).getByText(/It has used 87% of this month’s budget\. At 100%, calls charged to FIN/)).toBeVisible()
    expect(within(banners).getByText(/each cost center’s total, from everyone who charges it/)).toBeVisible()
  })

  it('says a cost center is over budget when its calls continue', async () => {
    renderPage([], {}, [
      { costCenter: finance, level: 'exceeded', month: '2026-03', used: 1.124, action: 'continue' },
    ])

    expect(await screen.findByText('Finance is over its monthly budget')).toBeVisible()
    expect(screen.getByText(/It has used 112% of this month’s budget\. Calls charged to FIN still work\./)).toBeVisible()
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
    expect(screen.getByText('Production')).toBeVisible()
    expect(screen.getByText('Production gateway')).toBeVisible()
    expect(screen.getByText('10,000 tokens per minute')).toBeVisible()
    expect(screen.getByText('600 calls per 60 seconds')).toBeVisible()
    expect(screen.getByText('100,000 calls per month')).toBeVisible()
    expect(screen.getByText("Usage can't be measured for this grant yet.")).toBeVisible()
    expect(screen.getByText('Recorded grant')).toBeVisible()
  })

  it('shows one card for each cost center grant and keeps matching resources adjacent', async () => {
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

    expect(await screen.findAllByRole('heading', { level: 2, name: 'Chat completions' })).toHaveLength(2)
    const cards = Array.from(document.querySelectorAll('.access-card')) as HTMLElement[]
    expect(within(cards[0]).getByText('Finance · FIN')).toBeVisible()
    expect(within(cards[1]).getByText('Research · RES')).toBeVisible()
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
    expect(await screen.findByText(label)).toBeVisible()
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
    expect(screen.getByText('The gateway records each call you make with this grant, so your own usage can be measured.')).toBeVisible()
    expect(screen.getByText('The gateway records each call made with this grant, so its usage can be measured.')).toBeVisible()
    expect(
      screen.getByText('Linked through APIM subscription grant-subscription in product model-product.'),
    ).toBeVisible()
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
    const details = screen.getAllByRole('button', { name: 'Connection details' })
    expect(details).toHaveLength(2)
    expect(details[0]).toHaveAttribute('aria-expanded', 'false')
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
    expect(api.getMcpConnection).not.toHaveBeenCalled()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
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

    expect(await screen.findByText(label)).toBeVisible()
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

    const card = (await screen.findByText('Retired chat')).closest('.access-card')
    expect(card).not.toBeNull()
    expect(within(card as HTMLElement).getByText('No longer available')).toBeVisible()
    expect(within(card as HTMLElement).getByText('Model API · Granted directly to you')).toBeVisible()
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
    expect(screen.getByText('Model API · Granted through Platform engineering')).toBeVisible()
    expect(screen.getByRole('heading', { level: 2, name: 'Docs search' })).toBeVisible()
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
