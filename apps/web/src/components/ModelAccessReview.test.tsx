import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { accessPlan, accessSnapshot } from '../test/model-access'
import type { PublishPlan } from '../types'
import { ModelAccessReview } from './ModelAccessReview'

const api = {
  listPrincipals: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderReview(plan: PublishPlan) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ModelAccessReview plan={plan} />
    </QueryClientProvider>,
  )
}

describe('ModelAccessReview', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listPrincipals.mockResolvedValue([])
  })

  it('uses principal kinds from the directory and falls back to subject labels', async () => {
    api.listPrincipals.mockResolvedValue([
      { id: 'app_1', tenantId: 'tenant', objectId: 'app-object', kind: 'agentIdentity', label: 'Agent identity', createdAt: '', updatedAt: '' },
    ])
    renderReview({
      ...accessPlan,
      accessSnapshot: {
        ...accessSnapshot,
        grants: [
          { ...accessSnapshot.grants[0], entitlementId: 'agent_grant', subject: { kind: 'application', id: 'app_1' }, objectId: 'agent-object', displayName: 'Agent app', subscriptionName: null },
          { ...accessSnapshot.grants[0], entitlementId: 'fallback_grant', subject: { kind: 'application', id: 'missing' }, objectId: 'missing-object', displayName: 'Unknown app', subscriptionName: null },
        ],
      },
    })

    expect(await screen.findByText('Agent · agent-object')).toBeVisible()
    expect(screen.getByText('Application · missing-object')).toBeVisible()
  })

  it('shows empty text and token-only subscriptions clearly', async () => {
    renderReview({ ...accessPlan, accessSnapshot: { ...accessSnapshot, grants: [] } })
    expect(await screen.findByText('No grants are in this target. No caller will be authorized.')).toBeVisible()

    renderReview({
      ...accessPlan,
      accessSnapshot: {
        ...accessSnapshot,
        grants: [{ ...accessSnapshot.grants[0], subscriptionName: null }],
      },
    })
    const table = await screen.findByRole('table', { name: 'All target model grants' })
    expect(within(table).getByText('None (Entra token)')).toBeVisible()
  })
})
