import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { DashboardPage } from './DashboardPage'

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    listPrincipals: async () => [
      {
        id: 'principal-user',
        tenantId: 'tenant',
        objectId: 'user-object',
        kind: 'user',
        label: 'User',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
      {
        id: 'principal-workload',
        tenantId: 'tenant',
        objectId: 'workload-object',
        kind: 'managedIdentity',
        label: 'Workload',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
    listGroups: async () => [
      {
        id: 'group',
        tenantId: 'tenant',
        name: 'Engineering',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
  }),
}))

describe('DashboardPage', () => {
  it('separates live desired state from sample telemetry', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByText('registered users')).toBeVisible()
    expect(screen.getByText('workload identities')).toBeVisible()
    expect(screen.getByText('access groups')).toBeVisible()
    expect(screen.getByText('Live data')).toBeVisible()
    expect(screen.getAllByText('Sample data').length).toBeGreaterThan(1)
    expect(screen.getByRole('note')).toHaveTextContent('MOSAIC is not querying Azure Monitor yet')
  })
})
