import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { Entitlement, ResolvedEntitlement } from '../types'
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

function renderPage(entitlements: ResolvedEntitlement[]) {
  const api = {
    listEntitlements: async () => entitlements,
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
        via: 'group',
        viaGroupId: 'group-1',
        viaGroupName: 'Platform engineering',
      },
    ])

    expect(await screen.findByText('Model API chat-completions')).toBeVisible()
    expect(screen.getByText('Granted through Platform engineering')).toBeVisible()
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
    const details = screen.getAllByRole('button', { name: 'Connection details' })
    expect(details).toHaveLength(1)
    expect(details[0]).toHaveAttribute('aria-expanded', 'false')
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
  })

  it('heads each grant with the name the catalog shows and keeps its kind visible', async () => {
    renderPage([
      {
        entitlement: baseEntitlement,
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
    'falls back to the kind and ID when %s',
    async (_, name) => {
      renderPage([
        {
          entitlement: baseEntitlement,
          via: 'group',
          viaGroupId: 'group-1',
          viaGroupName: 'Platform engineering',
          ...name,
        },
      ])

      expect(
        await screen.findByRole('heading', { level: 2, name: 'Model API chat-completions' }),
      ).toBeVisible()
      expect(screen.getByText('Granted through Platform engineering')).toBeVisible()
    },
  )
})
