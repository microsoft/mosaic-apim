import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { Entitlement } from '../types'
import { describeLimits } from '../entitlement-limits'
import { EntitlementsPage } from './EntitlementsPage'

const entitlement: Entitlement = {
  id: 'entitlement_1',
  tenantId: 'tenant',
  subject: { kind: 'group', id: 'group_1' },
  resource: { kind: 'modelApi', id: 'modelApi_1' },
  enabled: true,
  enforcement: {
    tokens: {
      counterKeyExpression: '@(context.Subscription?.Key)',
      tokensPerMinute: 10000,
      tokenQuota: 5000000,
      tokenQuotaPeriod: 'Monthly',
      estimatePromptTokens: true,
    },
  },
  binding: null,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
}

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    listEntitlements: async () => [entitlement],
    listPrincipals: async () => [
      {
        id: 'principal_1',
        tenantId: 'tenant',
        objectId: 'object-1',
        kind: 'user',
        label: 'Ada Lovelace',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
    listGroups: async () => [
      {
        id: 'group_1',
        tenantId: 'tenant',
        name: 'Engineering',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
    listModelApis: async () => [
      {
        id: 'modelApi_1',
        tenantId: 'tenant',
        gatewayId: 'gateway_1',
        apiName: 'chat',
        displayName: 'Chat completions',
        path: 'chat',
        protocols: [],
        aiKind: 'azureOpenAi',
        aiSignals: [],
        subscriptionRequired: true,
        operationCount: 2,
        productNames: [],
        visibility: 'catalog',
        selection: 'detected',
        importedFromSnapshotId: 'snapshot_1',
        importedAt: '2026-01-01T00:00:00Z',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
    listMcpServers: async () => [],
    listAccessRequests: async () => [],
    resolveEntitlements: async () => [],
  }),
}))

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <EntitlementsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('EntitlementsPage', () => {
  it('renders live grants with their subject, resource, and binding state', async () => {
    renderPage()

    expect(await screen.findByText('Engineering (group)')).toBeVisible()
    expect(screen.getByText('Chat completions (model API)')).toBeVisible()
    // A grant with no binding must say so: consumption cannot be attributed without one.
    expect(screen.getByText('Not bound')).toBeVisible()
    expect(screen.getByText('Live data')).toBeVisible()
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
  })

  it('states limits as sentences rather than policy markup', async () => {
    renderPage()

    expect(await screen.findByText('Limits usage to 10,000 tokens per minute.')).toBeVisible()
    expect(screen.getByText('Allows 5,000,000 tokens per month.')).toBeVisible()
  })
})

describe('describeLimits', () => {
  it('reports an unrestricted grant honestly instead of showing a limit of zero', () => {
    expect(describeLimits({ ...entitlement, enforcement: null })).toEqual([
      'No limit is configured. This grant is unrestricted.',
    ])
    expect(describeLimits({ ...entitlement, enforcement: {} })).toEqual([
      'No limit is configured. This grant is unrestricted.',
    ])
  })

  it('describes request limits separately from token limits', () => {
    expect(
      describeLimits({
        ...entitlement,
        enforcement: {
          requests: {
            counterKeyExpression: '@(context.Subscription?.Key)',
            calls: 60,
            renewalPeriodSeconds: 60,
            callQuota: 100000,
            callQuotaPeriod: 'Monthly',
          },
        },
      }),
    ).toEqual(['Limits traffic to 60 calls per minute.', 'Allows 100,000 calls per month.'])
  })
})
