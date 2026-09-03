import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { EntitlementsPage } from './EntitlementsPage'

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    listGroups: async () => [
      {
        id: 'live-engineering',
        tenantId: 'tenant',
        name: 'Engineering',
        description: 'Live engineering group',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
  }),
}))

describe('EntitlementsPage', () => {
  it('binds browser-only grant previews to live MOSAIC groups', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <EntitlementsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect((await screen.findAllByText('Engineering')).length).toBeGreaterThan(0)
    expect(screen.getByText('Live engineering group')).toBeVisible()
    expect(screen.queryByText('group-platform')).not.toBeInTheDocument()
    expect(screen.getAllByText('Local preview').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Add entitlement' })).toBeEnabled()
  })
})
