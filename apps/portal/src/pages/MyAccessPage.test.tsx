import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { Entitlement, ResolvedEntitlement, ResourceSummary } from '../types'
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

function renderPage(entitlements: ResolvedEntitlement[]) {
  const api = {
    listEntitlements: async () => entitlements,
    listEnvironments: async () => [
      { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, order: 50 },
    ],
    getMyEntitlementConnection: vi.fn(),
    revealMyEntitlementKey: vi.fn(),
  }
  mocks.api = api as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/access']}>
        <MyAccessPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return api
}

describe('MyAccessPage', () => {
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
    expect(screen.getByText('Granted through Platform engineering')).toBeVisible()
    expect(screen.getByText('Production')).toBeVisible()
    expect(screen.getByText('Production gateway')).toBeVisible()
    expect(screen.getByText('10,000 tokens per minute')).toBeVisible()
    expect(screen.getByText('600 calls per 60 seconds')).toBeVisible()
    expect(screen.getByText('100,000 calls per month')).toBeVisible()
    expect(screen.getByText('No usage attribution is configured yet.')).toBeVisible()
    expect(screen.getByText('Recorded grant')).toBeVisible()
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
    expect(screen.getByText(/Publication and gateway limits may also apply/)).toBeVisible()
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
    expect(screen.getByText(/Product model-product/)).toBeVisible()
    expect(screen.getByText(/Subscription grant-subscription/)).toBeVisible()
    expect(screen.queryByText('Enabled')).not.toBeInTheDocument()
  })

  it('offers collapsed connection details for model API grants only', async () => {
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
    const details = screen.getAllByRole('button', { name: 'Connection details' })
    expect(details).toHaveLength(1)
    expect(details[0]).toHaveAttribute('aria-expanded', 'false')
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
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
    }])

    const card = (await screen.findByText('Retired chat')).closest('.access-card')
    expect(card).not.toBeNull()
    expect(within(card as HTMLElement).getByText('No longer available')).toBeVisible()
    expect(screen.queryByText(/modelApi_removed_123/)).not.toBeInTheDocument()
  })
})
