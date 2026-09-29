import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { IdentityPage } from './IdentityPage'

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    listPrincipals: async () => [
      {
        id: 'user',
        tenantId: 'tenant',
        objectId: 'entra-user',
        kind: 'user',
        label: 'Alex User',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
      {
        id: 'workload',
        tenantId: 'tenant',
        objectId: 'entra-workload',
        kind: 'managedIdentity',
        label: 'Build agent',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
      {
        id: 'group-principal',
        tenantId: 'tenant',
        objectId: 'security-group-object',
        kind: 'securityGroup',
        label: 'Security Readers',
        directoryVerifiedAt: '2026-01-02T00:00:00Z',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
    getDirectoryStatus: async () => ({ lookupEnabled: true, groupClaimsEnabled: false }),
    searchDirectory: vi.fn(),
    listPrincipalMembers: async () => ({
      groupObjectId: 'security-group-object',
      truncated: true,
      members: [{
        objectId: 'member-object',
        kind: 'user',
        displayName: 'Member User',
        detail: 'member@example.com',
        principalId: 'user',
      }],
    }),
    listGroups: async () => [],
    listMemberships: async () => [],
    createPrincipal: vi.fn(),
    updatePrincipal: vi.fn(),
    deletePrincipal: vi.fn(),
    createGroup: vi.fn(),
    updateGroup: vi.fn(),
    deleteGroup: vi.fn(),
    addMembership: vi.fn(),
    removeMembership: vi.fn(),
  }),
}))

describe('IdentityPage', () => {
  it('separates users from workload identities using URL-addressable tabs', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/identity?tab=users']}>
          <IdentityPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Build agent')).not.toBeInTheDocument()
    expect(screen.getByText('Live data')).toBeVisible()

    await user.click(screen.getByRole('tab', { name: 'Applications and security groups' }))

    expect((await screen.findAllByText('Build agent')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Alex User')).not.toBeInTheDocument()
    expect(screen.getByText(/Security-group grants are not enforceable yet/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: /Security Readers/ }))
    expect(await screen.findByText('Member User')).toBeVisible()
    expect(screen.getByText('Recorded in MOSAIC')).toBeVisible()
    expect(screen.getByText(/member list is truncated/)).toBeVisible()
  })
})
