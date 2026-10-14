import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation, useNavigate } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Group, Principal } from '../types'
import { IdentityPage } from './IdentityPage'

const mocks = vi.hoisted(() => ({
  lookupEnabled: true,
  statusError: false,
  getDirectoryStatus: vi.fn(),
  principals: [] as Principal[],
  groups: [] as Group[],
  mcpPublications: [] as Array<{ id: string; displayName: string; modelCallerId?: string | null }>,
  searchDirectory: vi.fn(),
  createPrincipal: vi.fn(),
  createGroup: vi.fn(),
  updatePrincipal: vi.fn(),
  updateGroup: vi.fn(),
}))

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {},
  useMosaicApi: () => ({
    listPrincipals: async () => [...mocks.principals],
    getDirectoryStatus: mocks.getDirectoryStatus,
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
    listGroups: async () => [...mocks.groups],
    listCostCenters: async () => [
      { id: 'cc-general', name: 'General', code: 'general' },
      { id: 'cc-support', name: 'Support', code: 'support' },
    ],
    getCostCenterSettings: async () => ({ id: 'settings', tenantId: 'tenant', defaultCostCenterId: 'cc-general' }),
    listMemberships: async () => [],
    listMcpPublications: async () => [...mocks.mcpPublications],
    createPrincipal: mocks.createPrincipal,
    updatePrincipal: mocks.updatePrincipal,
    deletePrincipal: vi.fn(),
    createGroup: mocks.createGroup,
    updateGroup: mocks.updateGroup,
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
      <RouterProvider router={createMemoryRouter([{
        path: '*',
        element: <><IdentityPage /><NavigationProbe /></>,
      }], { initialEntries: [path] })} />
    </QueryClientProvider>,
  )
}

function NavigationProbe() {
  const navigate = useNavigate()
  const location = useLocation()
  return <>
    <button onClick={() => navigate(-1)}>Previous section</button>
    <output aria-label="Section query">{location.search}</output>
  </>
}

function principalRow(name: string | RegExp) {
  return screen.getByRole('button', { name })
}

function sectionButton(name: string) {
  return within(screen.getByRole('navigation', { name: 'Identity sections', hidden: true }))
    .getByRole('button', { name, hidden: true })
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
    mocks.statusError = false
    mocks.getDirectoryStatus.mockImplementation(async () => {
      if (mocks.statusError) throw new Error('Directory status unavailable')
      return { lookupEnabled: mocks.lookupEnabled, groupClaimsEnabled: false }
    })
    mocks.principals = seedPrincipals()
    mocks.groups = []
    mocks.mcpPublications = []
  })

  it('opens persistent details only on selection and protects workspace edits', async () => {
    const user = userEvent.setup()
    renderPage()
    const row = await screen.findByRole('button', { name: /Alex User/ })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(row)
    expect(await screen.findByRole('region', { name: 'Alex User' })).toBeVisible()
    expect(screen.getByRole('list', { name: 'People' })).toBeVisible()
    expect(screen.queryByRole('textbox', { name: 'Local label' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit local metadata' }))
    expect(screen.getByRole('textbox', { name: 'Local label' })).toHaveFocus()
    await user.type(screen.getByRole('textbox', { name: 'Local label' }), ' changed')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByRole('textbox', { name: 'Local label' })).toHaveValue('Alex User changed')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(mocks.updatePrincipal).not.toHaveBeenCalled()
  })

  it('shows failed directory availability instead of silently opening manual entry', async () => {
    const user = userEvent.setup()
    mocks.statusError = true
    renderPage()
    await screen.findByRole('button', { name: /Alex User/ })
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    expect(await screen.findByText('Could not check directory availability.')).toBeVisible()
    expect(screen.queryByRole('textbox', { name: 'Entra object ID' })).not.toBeInTheDocument()
    mocks.statusError = false
    await user.click(screen.getByRole('button', { name: 'Retry directory availability' }))
    expect(await screen.findByRole('textbox', { name: 'Directory search' })).toBeVisible()
  })

  it('keeps local group overview separate from editing and membership actions', async () => {
    const user = userEvent.setup()
    mocks.groups = [{ id: 'group-one', tenantId: 'tenant', name: 'Research team', description: 'Local collection', ...timestamps }]
    renderPage('/identity?tab=groups')
    const row = await screen.findByRole('button', { name: /Research team/ })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(row)
    expect(await screen.findByRole('region', { name: 'Research team' })).toBeVisible()
    expect(screen.queryByRole('textbox', { name: 'Description' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Add principal' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit group' }))
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveValue('Local collection')
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveFocus()
    expect(screen.getByRole('combobox', { name: 'Add principal' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Cancel editing' }))
    expect(screen.queryByRole('textbox', { name: 'Description' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit group' })).toHaveFocus()
  })

  describe.each([
    {
      kind: 'principal',
      tab: 'users',
      section: 'People',
      row: /Alex User/,
      edit: 'Edit local metadata',
      field: 'Local label',
      value: 'Alex User',
    },
    {
      kind: 'local group',
      tab: 'groups',
      section: 'MOSAIC groups',
      row: /Research team/,
      edit: 'Edit group',
      field: 'Description',
      value: 'Local collection',
    },
  ])('$kind section navigation', ({ tab, section, row, edit, field, value }) => {
    beforeEach(() => {
      mocks.groups = [{ id: 'group-one', tenantId: 'tenant', name: 'Research team', description: 'Local collection', ...timestamps }]
    })

    it.each(['buttons', 'selector'])('guards dirty drafts from section %s until discard is confirmed', async (control) => {
      const user = userEvent.setup()
      renderPage(`/identity?tab=${tab}&scope=kept`)
      await user.click(await screen.findByRole('button', { name: row }))
      await user.click(screen.getByRole('button', { name: edit }))
      await user.type(screen.getByRole('textbox', { name: field }), ' changed')

      // Both rail and compact selector use the same guarded URL transition.
      async function selectAgents() {
        await act(async () => {
          if (control === 'buttons') {
            fireEvent.click(sectionButton('Agents'))
          } else {
            fireEvent.change(screen.getByLabelText('Identity section'), { target: { value: 'agents' } })
          }
        })
      }

      await selectAgents()
      expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
      expect(screen.getByLabelText('Section query')).toHaveTextContent(`?tab=${tab}&scope=kept`)
      expect(sectionButton(section)).toHaveAttribute('aria-current', 'page')

      await user.click(screen.getByRole('button', { name: 'Stay' }))
      expect(await screen.findByRole('textbox', { name: field })).toHaveValue(`${value} changed`)
      expect(screen.getByLabelText('Section query')).toHaveTextContent(`?tab=${tab}&scope=kept`)
      expect(sectionButton(section)).toHaveAttribute('aria-current', 'page')

      await selectAgents()
      await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
      expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(screen.getByLabelText('Section query')).toHaveTextContent('?tab=agents&scope=kept')
      expect(mocks.updatePrincipal).not.toHaveBeenCalled()
      expect(mocks.updateGroup).not.toHaveBeenCalled()

      await user.click(sectionButton(section))
      await user.click(await screen.findByRole('button', { name: row }))
      await user.click(screen.getByRole('button', { name: edit }))
      expect(screen.getByRole('textbox', { name: field })).toHaveValue(value)
    })

    it.each(['overview', 'editing'])('navigates without a prompt from a clean %s', async (mode) => {
      const user = userEvent.setup()
      renderPage(`/identity?tab=${tab}&scope=kept`)
      await user.click(await screen.findByRole('button', { name: row }))
      if (mode === 'editing') {
        await user.click(screen.getByRole('button', { name: edit }))
      }

      await act(async () => { fireEvent.click(sectionButton('Agents')) })
      expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(screen.getByLabelText('Section query')).toHaveTextContent('?tab=agents&scope=kept')
    })

    it('does nothing when the current section is selected with a dirty draft', async () => {
      const user = userEvent.setup()
      renderPage(`/identity?tab=${tab}&scope=kept`)
      await user.click(await screen.findByRole('button', { name: row }))
      await user.click(screen.getByRole('button', { name: edit }))
      await user.type(screen.getByRole('textbox', { name: field }), ' changed')

      await act(async () => { fireEvent.click(sectionButton(section)) })
      expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument()
      expect(screen.getByRole('textbox', { name: field })).toHaveValue(`${value} changed`)
      expect(screen.getByLabelText('Section query')).toHaveTextContent(`?tab=${tab}&scope=kept`)
    })

    it.each(['Stay', 'Escape'])('forgets canceled section navigation when returning to details with %s', async (dismissal) => {
      const user = userEvent.setup()
      renderPage(`/identity?tab=${tab}&scope=kept`)
      await user.click(await screen.findByRole('button', { name: row }))
      await user.click(screen.getByRole('button', { name: edit }))
      await user.type(screen.getByRole('textbox', { name: field }), ' changed')
      await act(async () => { fireEvent.click(sectionButton('Agents')) })
      const confirmation = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })
      await waitFor(() => expect(within(confirmation).getByRole('button', { name: 'Stay' })).toHaveFocus())
      if (dismissal === 'Escape') {
        await user.keyboard('{Escape}')
      } else {
        await user.click(screen.getByRole('button', { name: 'Stay' }))
      }
      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument())
      const draft = await screen.findByRole('textbox', { name: field })
      expect(draft).toHaveValue(`${value} changed`)
      await waitFor(() => expect(draft).toHaveFocus())

      await user.click(await screen.findByRole('button', { name: 'Back' }))
      await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(await screen.findByRole('heading', { name: section })).toBeVisible()
      expect(screen.getByLabelText('Section query')).toHaveTextContent(`?tab=${tab}&scope=kept`)
    })

    it.each(['Stay', 'Escape'])('restores focus to the draft when leaving the editor is canceled with %s', async (dismissal) => {
      const user = userEvent.setup()
      renderPage(`/identity?tab=${tab}&scope=kept`)
      await user.click(await screen.findByRole('button', { name: row }))
      await user.click(screen.getByRole('button', { name: edit }))
      await user.type(screen.getByRole('textbox', { name: field }), ' changed')
      await user.click(screen.getByRole('button', { name: 'Back' }))
      const confirmation = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })
      await waitFor(() => expect(within(confirmation).getByRole('button', { name: 'Stay' })).toHaveFocus())
      if (dismissal === 'Escape') {
        await user.keyboard('{Escape}')
      } else {
        await user.click(screen.getByRole('button', { name: 'Stay' }))
      }

      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument())
      const draft = await screen.findByRole('textbox', { name: field })
      expect(draft).toHaveValue(`${value} changed`)
      await waitFor(() => expect(draft).toHaveFocus())
      expect(sectionButton(section)).toHaveAttribute('aria-current', 'page')
      expect(screen.getByLabelText('Section query')).toHaveTextContent(`?tab=${tab}&scope=kept`)
      expect(mocks.updatePrincipal).not.toHaveBeenCalled()
      expect(mocks.updateGroup).not.toHaveBeenCalled()
    })
  })

  it('lists people, agents, and applications with security groups on their own tabs', async () => {
    const user = userEvent.setup()
    renderPage()

    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Benefits Bot')).not.toBeInTheDocument()
    expect(screen.queryByText('Build agent')).not.toBeInTheDocument()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Agents' }))

    expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()
    expect(principalRow(/Benefits Bot mailbox/)).toBeVisible()
    expect(screen.getByText('Parent agent Benefits Bot')).toBeVisible()
    expect(screen.getByText('Blueprint benefits-blueprint')).toBeVisible()
    expect(screen.queryByText('Alex User')).not.toBeInTheDocument()
    expect(screen.queryByText('Build agent')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add agent' })).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Applications & managed identities' }))

    expect((await screen.findAllByText('Build agent')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Alex User')).not.toBeInTheDocument()
    expect(screen.queryByText('Benefits Bot mailbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Security Readers/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Entra security groups' }))
    expect(screen.getByText(/Security-group grants are not enforceable yet/)).toBeVisible()
    await user.click(principalRow(/Security Readers/))
    expect(await screen.findByText('Member User')).toBeVisible()
    expect(screen.getByText('Recorded in MOSAIC')).toBeVisible()
    expect(screen.getByText(/member list is truncated/)).toBeVisible()
  })

  it('marks applications that MCP servers call models as', async () => {
    const user = userEvent.setup()
    mocks.mcpPublications = [
      { id: 'mcp_pub_search', displayName: 'Contoso Search', modelCallerId: 'workload' },
      { id: 'mcp_pub_docs', displayName: 'Contoso Docs', modelCallerId: 'workload' },
    ]

    renderPage('/identity?tab=workloads')

    expect(await screen.findByRole('heading', { name: 'Applications & managed identities' })).toBeVisible()
    // Named in the same order whatever order the publications arrive in.
    expect(screen.getAllByText('Calls models for Contoso Docs, Contoso Search').length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Entra security groups' }))
    await user.click(await screen.findByRole('button', { name: /Security Readers/ }))
    // Only the application's own row keeps its badge; Security Readers' detail panel has none.
    expect(screen.queryByText('Calls models for Contoso Docs, Contoso Search')).not.toBeInTheDocument()
  })

  it('opens Add person on directory search and switches to manual entry and back', async () => {
    const user = userEvent.setup()
    renderPage()
    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: 'Add person' }))
    let dialog = await screen.findByRole('region', { name: 'Add person' })
    expect(await within(dialog).findByRole('textbox', { name: 'Directory search' })).toBeVisible()
    expect(within(dialog).getByRole('combobox', { name: 'Directory search kind' })).toHaveValue('user')
    expect(within(dialog).queryByRole('textbox', { name: /Entra object ID/ })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('region', { name: 'Add person' })
    expect(within(dialog).getByRole('textbox', { name: /Entra object ID/ })).toBeVisible()
    expect(within(dialog).getByRole('combobox', { name: 'Principal type' })).toHaveValue('user')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(within(dialog).queryByRole('textbox', { name: 'Directory search' })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Search the directory' }))
    dialog = screen.getByRole('region', { name: 'Add person' })
    expect(within(dialog).getByRole('textbox', { name: 'Directory search' })).toBeVisible()
  })

  it('opens Add agent on an agent search, and manual entry keeps the agent type', async () => {
    const user = userEvent.setup()
    mocks.searchDirectory.mockResolvedValue([])
    renderPage('/identity?tab=agents')
    expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Add agent' }))
    let dialog = await screen.findByRole('region', { name: 'Add agent' })
    expect(within(dialog).getByRole('combobox', { name: 'Directory search kind' })).toHaveValue('agent')
    await user.type(within(dialog).getByRole('textbox', { name: 'Directory search' }), 'bot')
    await waitFor(() => expect(mocks.searchDirectory).toHaveBeenCalledWith('agent', 'bot', 20))

    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Directory search kind' }), 'group')
    expect(screen.getByRole('region', { name: 'Add security group' })).toBeVisible()
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Directory search kind' }), 'agent')

    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('region', { name: 'Add agent' })
    expect(within(dialog).getByRole('combobox', { name: 'Principal type' })).toHaveValue('agentIdentity')
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Principal type' }), 'agentUser')
    expect(screen.getByRole('region', { name: 'Add agent user' })).toBeVisible()
  })

  it('maps the old workloads link to applications and exposes independent Entra and MOSAIC group sections', async () => {
    const user = userEvent.setup()
    renderPage('/identity?tab=workloads&scope=kept')
    expect(await screen.findByRole('heading', { name: 'Applications & managed identities' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /Security Readers/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add application or managed identity' }))
    const dialog = await screen.findByRole('region', { name: 'Add application or managed identity' })
    expect(within(dialog).getByRole('combobox', { name: 'Directory search kind' })).toHaveValue('application')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: /^Add / })).not.toBeInTheDocument())
    await user.click(await screen.findByRole('button', { name: 'MOSAIC groups' }))
    expect(screen.getByText(/does not create or change an Entra group/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Create MOSAIC group' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add identity' })).not.toBeInTheDocument()
  })

  it('supports browser Back between actual views and retains other query values', async () => {
    const user = userEvent.setup()
    renderPage('/identity?tab=users&scope=kept')
    expect(await screen.findByRole('heading', { name: 'People' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Agents' }))
    expect(await screen.findByRole('heading', { name: 'Agents' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'People' })).not.toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Section query' })).toHaveTextContent('scope=kept')
    await user.click(screen.getByRole('button', { name: 'Previous section' }))
    expect(await screen.findByRole('heading', { name: 'People' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Agents' })).not.toBeInTheDocument()
  })

  it('does not claim directory lookup is unavailable while checking its status', async () => {
    mocks.getDirectoryStatus.mockReturnValue(new Promise(() => {}))
    const user = userEvent.setup()
    renderPage()
    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: 'Add person' }))
    const dialog = await screen.findByRole('region', { name: 'Add person' })
    expect(within(dialog).getByText('Checking directory lookup availability')).toBeVisible()
    expect(within(dialog).queryByText(/Directory lookup requires Entra authentication/)).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Use manual entry' })).not.toBeInTheDocument()
  })

  it('keeps Add person search-first with explicit manual fallback for local authentication', async () => {
    mocks.lookupEnabled = false
    const user = userEvent.setup()
    renderPage()
    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: 'Add person' }))
    const dialog = await screen.findByRole('region', { name: 'Add person' })
    expect(await within(dialog).findByText(/Directory lookup requires Entra authentication/)).toBeVisible()
    expect(within(dialog).queryByText(/MOSAIC_ENTRA_DIRECTORY_LOOKUP/)).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('textbox', { name: /Entra object ID/ })).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    expect(within(dialog).getByRole('textbox', { name: /Entra object ID/ })).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Search the directory' })).toBeVisible()
    expect(within(dialog).queryByRole('textbox', { name: 'Directory search' })).not.toBeInTheDocument()
  })

  it('keeps the search dialog view while closing', async () => {
    const user = userEvent.setup()
    renderPage()
    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: 'Add person' }))
    const dialog = await screen.findByRole('region', { name: 'Add person' })
    expect(within(dialog).getByRole('textbox', { name: 'Directory search' })).toBeVisible()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('textbox', { name: /Entra object ID/ })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('region', { name: /^Add / })).not.toBeInTheDocument())
    expect(screen.queryByRole('textbox', { name: /Entra object ID/ })).not.toBeInTheDocument()
  })

  it('clears a non-matching filter after manual add so the new record is selected', { timeout: 10_000 }, async () => {
    const user = userEvent.setup()
    mockCreatePrincipal()
    renderPage()
    const filter = (await screen.findByRole('textbox', {
      name: 'Filter by label or object ID',
    })) as HTMLInputElement

    await user.type(filter, 'zzz')
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    let dialog = await screen.findByRole('region', { name: 'Add person' })
    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('region', { name: 'Add person' })
    await user.type(within(dialog).getByRole('textbox', { name: /Entra object ID/ }), 'entra-new-person')
    await user.type(within(dialog).getByRole('textbox', { name: 'Local label' }), 'Nia Person')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('region', { name: /^Add / })).not.toBeInTheDocument())
    expect(filter).toHaveValue('')
    expect(await screen.findByRole('button', { name: /Nia Person/, pressed: true })).toBeVisible()
  })

  it('keeps a matching filter after manual add', { timeout: 10_000 }, async () => {
    const user = userEvent.setup()
    mockCreatePrincipal()
    renderPage()
    const filter = (await screen.findByRole('textbox', {
      name: 'Filter by label or object ID',
    })) as HTMLInputElement

    await user.type(filter, 'nia')
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    let dialog = await screen.findByRole('region', { name: 'Add person' })
    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('region', { name: 'Add person' })
    await user.type(within(dialog).getByRole('textbox', { name: /Entra object ID/ }), 'entra-new-person')
    await user.type(within(dialog).getByRole('textbox', { name: 'Local label' }), 'Nia Person')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('region', { name: /^Add / })).not.toBeInTheDocument())
    expect(filter).toHaveValue('nia')
    expect(await screen.findByRole('button', { name: /Nia Person/, pressed: true })).toBeVisible()
  })

  it('sends a default cost center when manually creating a person', { timeout: 10_000 }, async () => {
    const user = userEvent.setup()
    mockCreatePrincipal()
    renderPage()

    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    let dialog = await screen.findByRole('region', { name: 'Add person' })
    await user.click(within(dialog).getByRole('button', { name: 'Use manual entry' }))
    dialog = screen.getByRole('region', { name: 'Add person' })
    await user.type(within(dialog).getByRole('textbox', { name: /Entra object ID/ }), 'entra-new-person')
    await within(dialog).findByRole('option', { name: 'Support (support)' })
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Default cost center' }), 'cc-support')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(mocks.createPrincipal).toHaveBeenCalled())
    expect(mocks.createPrincipal.mock.calls[0][0]).toMatchObject({ defaultCostCenterId: 'cc-support' })
  })

  it('omits defaultCostCenterId for a directory person using the tenant default', async () => {
    const user = userEvent.setup()
    mocks.searchDirectory.mockResolvedValue([
      { objectId: 'entra-new-person', kind: 'user', displayName: 'Nia Person', detail: 'nia@example.com' },
    ])
    mockCreatePrincipal()
    renderPage()

    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Add person' }))
    const dialog = await screen.findByRole('region', { name: 'Add person' })
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Default cost center' }), '')
    await user.type(within(dialog).getByRole('textbox', { name: 'Directory search' }), 'nia')
    await user.click(await within(dialog).findByRole('button', { name: 'Select Nia Person' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add Nia Person' }))

    await waitFor(() => expect(mocks.createPrincipal).toHaveBeenCalled())
    expect(mocks.createPrincipal.mock.calls[0][0]).not.toHaveProperty('defaultCostCenterId')
  })

  it('shows and updates a principal default cost center', async () => {
    const user = userEvent.setup()
    mocks.principals = seedPrincipals().map((principal) =>
      principal.id === 'user' ? { ...principal, defaultCostCenterId: 'cc-general' } : principal,
    )
    mocks.updatePrincipal.mockImplementation(async (principalId: string, payload: Partial<Principal>) => {
      const current = mocks.principals.find((item) => item.id === principalId)
      if (!current) throw new Error(`Unknown principal ${principalId}`)
      const updated = { ...current, ...payload }
      mocks.principals = mocks.principals.map((item) => (item.id === principalId ? updated : item))
      return updated
    })
    renderPage()

    await user.click(await screen.findByRole('button', { name: /Alex User/ }))
    await user.click(screen.getByRole('button', { name: 'Edit local metadata' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Default cost center' }), 'cc-support')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(mocks.updatePrincipal).toHaveBeenCalledWith('user', expect.objectContaining({ defaultCostCenterId: 'cc-support' })))
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
    const dialog = await screen.findByRole('region', { name: 'Add person' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Directory search' }), 'nia')
    await user.click(await within(dialog).findByRole('button', { name: 'Select Nia Person' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add Nia Person' }))

    await waitFor(() => expect(screen.queryByRole('region', { name: /^Add / })).not.toBeInTheDocument())
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
    const dialog = await screen.findByRole('region', { name: 'Add agent' })
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Directory search kind' }), 'user')
    await user.type(within(dialog).getByRole('textbox', { name: 'Directory search' }), 'nia')
    await user.click(await within(dialog).findByRole('button', { name: 'Select Nia Person' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add Nia Person' }))

    await waitFor(() => expect(screen.queryByRole('region', { name: /^Add / })).not.toBeInTheDocument())
    await waitFor(() => expect(screen.getByRole('button', { name: 'People' })).toHaveAttribute('aria-current', 'page'))
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
    await user.click(await screen.findByRole('button', { name: /Build agent/, pressed: false }))
    await user.click(screen.getByRole('button', { name: 'Edit local metadata' }))

    // Recorded by hand as a managed identity, but it's an agent. It lands last on the Agents tab.
    await user.selectOptions(screen.getByRole('combobox', { name: 'Principal type' }), 'agentIdentity')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Agents' })).toHaveAttribute('aria-current', 'page'))
    expect(await screen.findByRole('button', { name: /Build agent/ })).toBeVisible()
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
    await user.click(await screen.findByRole('button', { name: /Build agent/, pressed: false }))
    await user.click(screen.getByRole('button', { name: 'Edit local metadata' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Principal type' }), 'agentIdentity')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Agents' })).toHaveAttribute('aria-current', 'page'))
    expect(screen.getByRole('textbox', { name: 'Filter by label, detail, object ID, or kind' })).toHaveValue('')
    expect(await screen.findByRole('button', { name: /Build agent/ })).toBeVisible()
  })

  it('restores a direct URL selection and guards browser Back from an unsaved workspace', async () => {
    const user = userEvent.setup()
    renderPage('/identity?selected=user&scope=kept')
    expect(await screen.findByRole('region', { name: 'Alex User' })).toBeVisible()
    expect(screen.getByRole('button', { name: /Alex User/, pressed: true })).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit local metadata' }))
    const draft = screen.getByRole('textbox', { name: 'Local label' })
    await user.type(draft, ' unsaved')
    await user.click(screen.getByRole('button', { name: 'Previous section' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(draft).toHaveValue('Alex User unsaved')
    await waitFor(() => expect(draft).toHaveFocus())
    await user.click(screen.getByRole('button', { name: 'Previous section' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('region', { name: 'Alex User' })).toBeVisible()
    expect(screen.queryByRole('textbox', { name: 'Local label' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Section query')).toHaveTextContent('selected=user')
    expect(screen.getByLabelText('Section query')).toHaveTextContent('scope=kept')
    expect(mocks.updatePrincipal).not.toHaveBeenCalled()
  })

  it('protects a manual onboarding draft and cannot leave while adding the reference', async () => {
    const user = userEvent.setup()
    let finish!: (principal: Principal) => void
    mocks.createPrincipal.mockReturnValue(new Promise<Principal>((resolve) => { finish = resolve }))
    renderPage('/identity?tab=users&mode=new')
    expect(await screen.findByRole('region', { name: 'Add person' })).toBeVisible()
    await user.click(await screen.findByRole('button', { name: 'Use manual entry' }))
    await user.type(screen.getByRole('textbox', { name: 'Entra object ID' }), 'entra-new')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByRole('textbox', { name: 'Entra object ID' })).toHaveValue('entra-new')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(mocks.createPrincipal).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByRole('dialog', { name: 'Operation in progress' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Discard changes' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    const created: Principal = { id: 'new-user', tenantId: 'tenant', objectId: 'entra-new', kind: 'user', label: 'New person', ...timestamps }
    mocks.principals.push(created)
    finish(created)
    expect(await screen.findByRole('button', { name: /New person/, pressed: true })).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Add person' })).not.toBeInTheDocument()
  })

  it('shows a recoverable state for missing selected references instead of stale details', async () => {
    renderPage('/identity?tab=users&selected=removed-reference')
    expect(await screen.findByText('Identity not in this view')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Clear selection and filter' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Edit local metadata' })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('creates and edits a MOSAIC group in full workspaces without changing its immutable name', async () => {
    const user = userEvent.setup()
    mocks.createGroup.mockImplementation(async (payload: { name: string; description: string }) => {
      const group: Group = { id: 'local-group', tenantId: 'tenant', ...payload, ...timestamps }
      mocks.groups.push(group)
      return group
    })
    mocks.updateGroup.mockImplementation(async (id: string, payload: { description: string }) => {
      mocks.groups = mocks.groups.map((group) => group.id === id ? { ...group, ...payload } : group)
      return mocks.groups.find((group) => group.id === id)
    })
    renderPage('/identity?tab=groups&scope=kept')
    await user.click(await screen.findByRole('button', { name: 'Create MOSAIC group' }))
    expect(await screen.findByRole('region', { name: 'Create group' })).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'Group name' }), 'Research team')
    await user.type(screen.getByRole('textbox', { name: 'Description' }), 'Local collection')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('button', { name: /Research team/, pressed: true })).toBeVisible()
    expect(screen.getByRole('region', { name: 'Research team' })).toBeVisible()
    expect(screen.getByLabelText('Section query')).toHaveTextContent('scope=kept')
    await user.click(screen.getByRole('button', { name: 'Edit group' }))
    expect(screen.getByRole('region', { name: 'Edit Research team' })).toBeVisible()
    expect(screen.queryByRole('list', { name: 'MOSAIC groups' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Group name' })).not.toBeInTheDocument()
    await user.clear(screen.getByRole('textbox', { name: 'Description' }))
    await user.type(screen.getByRole('textbox', { name: 'Description' }), 'Updated collection')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mocks.updateGroup).toHaveBeenCalledWith('local-group', { description: 'Updated collection' }))
    expect(await screen.findByRole('region', { name: 'Research team' })).toBeVisible()
    expect(screen.queryByRole('textbox', { name: 'Description' })).not.toBeInTheDocument()
  })
})
