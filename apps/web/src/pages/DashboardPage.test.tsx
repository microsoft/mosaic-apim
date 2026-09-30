import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { DashboardPage } from './DashboardPage'

const timestamps = { createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' }

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    listPrincipals: async () => [
      { id: 'principal-user', tenantId: 'tenant', objectId: 'user-object', kind: 'user', label: 'User', ...timestamps },
      { id: 'principal-agent', tenantId: 'tenant', objectId: 'agent-object', kind: 'agentIdentity', ...timestamps },
      { id: 'principal-agent-user', tenantId: 'tenant', objectId: 'agent-user-object', kind: 'agentUser', ...timestamps },
      { id: 'principal-workload', tenantId: 'tenant', objectId: 'workload-object', kind: 'managedIdentity', ...timestamps },
      { id: 'principal-group', tenantId: 'tenant', objectId: 'group-object', kind: 'securityGroup', ...timestamps },
      { id: 'principal-app', tenantId: 'tenant', objectId: 'app-object', kind: 'servicePrincipal', ...timestamps },
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

function inventoryCard(label: string) {
  const card = screen.getByText(label, { selector: 'small' }).closest('button')
  if (!card) {
    throw new Error(`No inventory card for ${label}`)
  }
  return card
}

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

    expect(await screen.findByText('person')).toBeVisible()
    expect(within(inventoryCard('person')).getByText('1')).toBeVisible()
    expect(within(inventoryCard('agents')).getByText('2')).toBeVisible()
    expect(within(inventoryCard('apps and security groups')).getByText('3')).toBeVisible()
    expect(within(inventoryCard('MOSAIC group')).getByText('1')).toBeVisible()
    expect(screen.getByText('Live data')).toBeVisible()
    expect(screen.getAllByText('Sample data').length).toBeGreaterThan(1)
    expect(screen.getByRole('note')).toHaveTextContent('MOSAIC is not querying Azure Monitor yet')
  })
})
