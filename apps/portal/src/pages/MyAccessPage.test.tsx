import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { Entitlement, PortalProfile, ResolvedEntitlement } from '../types'
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

function renderPage(entitlements: ResolvedEntitlement[], profileOverride: Partial<PortalProfile> = {}) {
  const api = {
    getProfile: async () => ({ ...profile, ...profileOverride }),
    listEntitlements: async () => entitlements,
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
  it('renders group attribution for entitlements', async () => {
    renderPage([
      {
        entitlement: baseEntitlement,
        via: 'group',
        viaGroupId: 'group-1',
        viaGroupName: 'Platform engineering',
      },
    ])

    expect(await screen.findByText('Model API chat-completions')).toBeVisible()
    expect(screen.getByText('Through Platform engineering')).toBeVisible()
    expect(screen.getByText('10,000 tokens per minute')).toBeVisible()
    expect(screen.getByText('600 calls per 60 seconds')).toBeVisible()
    expect(screen.getByText('100,000 calls per month')).toBeVisible()
    expect(screen.getByText('No usage attribution is configured yet.')).toBeVisible()
    expect(screen.getByText('Recorded grant')).toBeVisible()
  })

  it('renders security group attribution and per-person limit guidance', async () => {
    renderPage([
      {
        entitlement: {
          ...baseEntitlement,
          subject: { kind: 'securityGroup', id: 'principal-group-1' },
        },
        via: 'securityGroup',
        viaGroupId: 'principal-group-1',
        viaGroupName: 'AI builders',
      },
    ])

    expect(await screen.findByText('Model API chat-completions')).toBeVisible()
    expect(screen.getByText('Through AI builders')).toBeVisible()
    expect(screen.getByText(/Group access limits apply to each person individually\./)).toBeVisible()
  })

  it('uses the security-group subject when no via group name is returned', async () => {
    renderPage([
      {
        entitlement: {
          ...baseEntitlement,
          subject: { kind: 'securityGroup', id: 'principal-group-1' },
        },
        via: 'securityGroup',
        viaGroupId: null,
        viaGroupName: null,
      },
    ])

    expect(await screen.findByText('Through principal-group-1')).toBeVisible()
  })

  it('does not render defensive shadowed rows', async () => {
    renderPage([
      {
        entitlement: baseEntitlement,
        via: 'group',
        viaGroupId: 'group-1',
        viaGroupName: 'Platform engineering',
        effective: false,
        shadowedBy: 'entitlement-direct',
      },
    ])

    expect(await screen.findByText('No access granted yet')).toBeVisible()
    expect(screen.queryByText('Model API chat-completions')).not.toBeInTheDocument()
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
      via: 'direct',
      viaGroupId: null,
      viaGroupName: null,
    }])
    expect(await screen.findByText(label)).toBeVisible()
    expect(screen.getByText(/Product model-product/)).toBeVisible()
    expect(screen.getByText(/Subscription grant-subscription/)).toBeVisible()
    expect(screen.queryByText('Enabled')).not.toBeInTheDocument()
  })

  it('offers collapsed connection details for model API and MCP grants', async () => {
    const directUser = { kind: 'user' as const, id: 'user-1' }
    const api = renderPage([
      {
        entitlement: { ...baseEntitlement, id: 'model-grant', subject: directUser },
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
        via: 'direct',
        viaGroupId: null,
        viaGroupName: null,
      },
    ])

    expect(await screen.findByText('Model API chat-completions')).toBeVisible()
    expect(screen.getByText('MCP server docs-mcp')).toBeVisible()
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
      via: 'direct',
      viaGroupId: null,
      viaGroupName: null,
    }])

    expect(await screen.findByText(label)).toBeVisible()
  })
})
