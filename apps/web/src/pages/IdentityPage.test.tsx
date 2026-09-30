import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Principal } from '../types'
import { IdentityPage } from './IdentityPage'

const mocks = vi.hoisted(() => ({
  lookupEnabled: true,
  principals: [] as Principal[],
  searchDirectory: vi.fn(),
  createPrincipal: vi.fn(),
  updatePrincipal: vi.fn(),
}))

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {},
  useMosaicApi: () => ({
    listPrincipals: async () => [...mocks.principals],
    getDirectoryStatus: async () => ({
      lookupEnabled: mocks.lookupEnabled,
      groupClaimsEnabled: false,
    }),
    searchDirectory: mocks.searchDirectory,
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
    createPrincipal: mocks.createPrincipal,
    updatePrincipal: mocks.updatePrincipal,
    deletePrincipal: vi.fn(),
    createGroup: vi.fn(),
    updateGroup: vi.fn(),
    deleteGroup: vi.fn(),
    addMembership: vi.fn(),
    removeMembership: vi.fn(),
  }),
}))

const timestamps = { createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' }

function seedPrincipals(): Principal[] {
  return [
    { id: 'user', tenantId: 'tenant', objectId: 'entra-user', kind: 'user', label: 'Alex User', ...timestamps },
    {
      id: 'agent',
      tenantId: 'tenant',
      objectId: 'entra-agent',
      kind: 'agentIdentity',
      label: 'Benefits Bot',
      blueprintId: 'benefits-blueprint',
      ...timestamps,
    },
    {
      id: 'agent-user',
      tenantId: 'tenant',
      objectId: 'entra-agent-user',
      kind: 'agentUser',
      label: 'Benefits Bot mailbox',
      identityParentId: 'entra-agent',
      ...timestamps,
    },
    {
      id: 'workload',
      tenantId: 'tenant',
      objectId: 'entra-workload',
      kind: 'managedIdentity',
      label: 'Build agent',
      ...timestamps,
    },
    {
      id: 'group-principal',
      tenantId: 'tenant',
      objectId: 'security-group-object',
      kind: 'securityGroup',
      label: 'Security Readers',
      directoryVerifiedAt: '2026-01-02T00:00:00Z',
      ...timestamps,
    },
  ]
}

function renderPage(path = '/identity?tab=users') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <IdentityPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function principalRow(name: string | RegExp) {
  return screen.getByRole('button', { name })
}

function mockCreatePrincipal() {
  mocks.createPrincipal.mockImplementation(
    async (payload: { objectId: string; kind: Principal['kind']; label?: string }) => {
      const created: Principal = {
        id: `created-${payload.objectId}`,
        tenantId: 'tenant',
        objectId: payload.objectId,
        kind: payload.kind,
        label: payload.label,
        ...timestamps,
      }
      mocks.principals.push(created)
      return created
    },
  )
}

describe('IdentityPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.lookupEnabled = true
    mocks.principals = seedPrincipals()
  })

  it('lists people, agents, and applications with security groups on their own tabs', async () => {
    const user = userEvent.setup()
    renderPage()

    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Benefits Bot')).not.toBeInTheDocument()
    expect(screen.queryByText('Build agent')).not.toBeInTheDocument()
    expect(screen.getByText('Live data')).toBeVisible()

    await user.click(screen.getByRole('tab', { name: 'Agents' }))

    expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()
    expect(principalRow(/Benefits Bot mailbox/)).toBeVisible()
    expect(screen.getByText('Parent agent Benefits Bot')).toBeVisible()
    expect(screen.getByText('Blueprint benefits-blueprint')).toBeVisible()
    expect(screen.queryByText('Alex User')).not.toBeInTheDocument()
    expect(screen.queryByText('Build agent')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add agent' })).toBeVisible()

    await user.click(screen.getByRole('tab', { name: 'Applications and security groups' }))

    expect((await screen.findAllByText('Build agent')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Alex User')).not.toBeInTheDocument()
    expect(screen.queryByText('Benefits Bot mailbox')).not.toBeInTheDocument()
    expect(screen.getByText(/Security-group grants are not enforceable yet/)).toBeVisible()
    await user.click(principalRow(/Security Readers/))
    expect(await screen.findByText('Member User')).toBeVisible()
    expect(screen.getByText('Recorded in MOSAIC')).toBeVisible()
    expect(screen.getByText(/member list is truncated/)).toBeVisible()
  })

  it('opens Add person on directory search and switches to manual entry and back', async () => {
    const user = userEvent.setup()
    renderPage()
    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: 'Add person' }))
    let dialog = await screen.findByRole('dialog', { name: 'Add person' })
    expect(await within(dialog).findByRole('textbox', { name: 'Directory search' })).toBeVisible()
    expect(within(dialog).getByRole('combobox', { name: 'Directory search kind' })).toHaveValue('user')
    expect(within(dialog).queryByRole('textbox', { name: /Entra object ID/ })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('dialog', { name: 'Add person' })
    expect(within(dialog).getByRole('textbox', { name: /Entra object ID/ })).toBeVisible()
    expect(within(dialog).getByRole('combobox', { name: 'Principal type' })).toHaveValue('user')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(within(dialog).queryByRole('textbox', { name: 'Directory search' })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Search the directory' }))
    dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('textbox', { name: 'Directory search' })).toBeVisible()
  })

  it('opens Add agent on an agent search, and manual entry keeps the agent type', async () => {
    const user = userEvent.setup()
    mocks.searchDirectory.mockResolvedValue([])
    renderPage('/identity?tab=agents')
    expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Add agent' }))
    let dialog = await screen.findByRole('dialog', { name: 'Add agent' })
    expect(within(dialog).getByRole('combobox', { name: 'Directory search kind' })).toHaveValue('agent')
    await user.type(within(dialog).getByRole('textbox', { name: 'Directory search' }), 'bot')
    await waitFor(() => expect(mocks.searchDirectory).toHaveBeenCalledWith('agent', 'bot', 20))

    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Directory search kind' }), 'group')
    expect(screen.getByRole('dialog', { name: 'Add security group' })).toBeVisible()
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Directory search kind' }), 'agent')

    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('dialog', { name: 'Add agent' })
    expect(within(dialog).getByRole('combobox', { name: 'Principal type' })).toHaveValue('agentIdentity')
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Principal type' }), 'agentUser')
    expect(screen.getByRole('dialog', { name: 'Add agent user' })).toBeVisible()
  })

  it('opens Add person on manual entry when directory lookup is off', async () => {
    mocks.lookupEnabled = false
    const user = userEvent.setup()
    renderPage()
    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: 'Add person' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add person' })
    expect(await within(dialog).findByText(/Directory lookup is off/)).toBeVisible()
    expect(within(dialog).getByRole('textbox', { name: /Entra object ID/ })).toBeVisible()
    expect(within(dialog).queryByRole('button', { name: 'Search the directory' })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('textbox', { name: 'Directory search' })).not.toBeInTheDocument()
  })

  it('keeps the search dialog view while closing', async () => {
    const user = userEvent.setup()
    renderPage()
    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: 'Add person' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add person' })
    expect(within(dialog).getByRole('textbox', { name: 'Directory search' })).toBeVisible()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('textbox', { name: /Entra object ID/ })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByRole('textbox', { name: /Entra object ID/ })).not.toBeInTheDocument()
  })

  it('clears a non-matching filter after manual add so the new record is selected', async () => {
    const user = userEvent.setup()
    mockCreatePrincipal()
    renderPage()
    const filter = (await screen.findByRole('textbox', {
      name: 'Filter by label or object ID',
    })) as HTMLInputElement

    await user.type(filter, 'zzz')
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    let dialog = await screen.findByRole('dialog', { name: 'Add person' })
    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('dialog', { name: 'Add person' })
    await user.type(within(dialog).getByRole('textbox', { name: /Entra object ID/ }), 'entra-new-person')
    await user.type(within(dialog).getByRole('textbox', { name: 'Local label' }), 'Nia Person')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(filter).toHaveValue('')
    expect(await screen.findByRole('button', { name: /Nia Person/, pressed: true })).toBeVisible()
  })

  it('keeps a matching filter after manual add', async () => {
    const user = userEvent.setup()
    mockCreatePrincipal()
    renderPage()
    const filter = (await screen.findByRole('textbox', {
      name: 'Filter by label or object ID',
    })) as HTMLInputElement

    await user.type(filter, 'nia')
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    let dialog = await screen.findByRole('dialog', { name: 'Add person' })
    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('dialog', { name: 'Add person' })
    await user.type(within(dialog).getByRole('textbox', { name: /Entra object ID/ }), 'entra-new-person')
    await user.type(within(dialog).getByRole('textbox', { name: 'Local label' }), 'Nia Person')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(filter).toHaveValue('nia')
    expect(await screen.findByRole('button', { name: /Nia Person/, pressed: true })).toBeVisible()
  })

  it('clears a non-matching filter after adding a directory search result', async () => {
    const user = userEvent.setup()
    mocks.searchDirectory.mockResolvedValue([
      { objectId: 'entra-new-person', kind: 'user', displayName: 'Nia Person', detail: 'nia@example.com' },
    ])
    mockCreatePrincipal()
    renderPage()
    const filter = (await screen.findByRole('textbox', {
      name: 'Filter by label or object ID',
    })) as HTMLInputElement

    await user.type(filter, 'zzz')
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add person' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Directory search' }), 'nia')
    await user.click(await within(dialog).findByRole('button', { name: 'Add Nia Person' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(filter).toHaveValue('')
    expect(await screen.findByRole('button', { name: /Nia Person/, pressed: true })).toBeVisible()
  })

  it('shows a directory result on its own tab once it is added', async () => {
    const user = userEvent.setup()
    mocks.searchDirectory.mockResolvedValue([
      { objectId: 'entra-new-person', kind: 'user', displayName: 'Nia Person', detail: 'nia@example.com' },
    ])
    mocks.createPrincipal.mockImplementation(async (payload: { objectId: string; label?: string }) => {
      const created: Principal = {
        id: 'new-person',
        tenantId: 'tenant',
        objectId: payload.objectId,
        kind: 'user',
        label: payload.label,
        ...timestamps,
      }
      mocks.principals.push(created)
      return created
    })
    renderPage('/identity?tab=agents')
    expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Add agent' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add agent' })
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Directory search kind' }), 'user')
    await user.type(within(dialog).getByRole('textbox', { name: 'Directory search' }), 'nia')
    await user.click(await within(dialog).findByRole('button', { name: 'Add Nia Person' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // The closed dialog's aria-hidden on the page lifts a moment later, so wait for the tab.
    expect(await screen.findByRole('tab', { name: 'People', selected: true })).toBeVisible()
    expect(await screen.findByRole('button', { name: /Nia Person/, pressed: true })).toBeVisible()
  })

  it('follows a principal to its new tab when its type changes', async () => {
    const user = userEvent.setup()
    mocks.updatePrincipal.mockImplementation(async (principalId: string, payload: { kind: Principal['kind'] }) => {
      const current = mocks.principals.find((item) => item.id === principalId)
      if (!current) {
        throw new Error(`Unknown principal ${principalId}`)
      }
      const updated = { ...current, kind: payload.kind }
      mocks.principals = mocks.principals.map((item) => (item.id === principalId ? updated : item))
      return updated
    })
    renderPage('/identity?tab=workloads')
    expect(await screen.findByRole('button', { name: /Build agent/, pressed: true })).toBeVisible()

    // Recorded by hand as a managed identity, but it's an agent. It lands last on the Agents tab.
    await user.selectOptions(screen.getByRole('combobox', { name: 'Principal type' }), 'agentIdentity')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByRole('tab', { name: 'Agents', selected: true })).toBeVisible()
    expect(await screen.findByRole('button', { name: /Build agent/, pressed: true })).toBeVisible()
    expect(screen.getByRole('button', { name: /Benefits Bot mailbox/, pressed: false })).toBeVisible()
  })

  it('clears a non-matching filter after retyping a principal to another tab', async () => {
    const user = userEvent.setup()
    mocks.updatePrincipal.mockImplementation(async (principalId: string, payload: { kind: Principal['kind'] }) => {
      const current = mocks.principals.find((item) => item.id === principalId)
      if (!current) {
        throw new Error(`Unknown principal ${principalId}`)
      }
      const updated = { ...current, kind: payload.kind }
      mocks.principals = mocks.principals.map((item) => (item.id === principalId ? updated : item))
      return updated
    })
    renderPage('/identity?tab=workloads')
    const filter = (await screen.findByRole('textbox', {
      name: 'Filter by label, detail, object ID, or kind',
    })) as HTMLInputElement

    await user.type(filter, 'managed')
    expect(await screen.findByRole('button', { name: /Build agent/, pressed: true })).toBeVisible()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Principal type' }), 'agentIdentity')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByRole('tab', { name: 'Agents', selected: true })).toBeVisible()
    expect(filter).toHaveValue('')
    expect(await screen.findByRole('button', { name: /Build agent/, pressed: true })).toBeVisible()
  })
})
