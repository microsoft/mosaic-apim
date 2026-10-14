import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DirectoryPrincipalPicker } from './DirectoryPrincipalPicker'

const mocks = vi.hoisted(() => {
  class TestApiError extends Error {
    status: number
    body?: { code?: string; message?: string }

    constructor(message: string, status: number, body?: { code?: string; message?: string }) {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    TestApiError,
    api: {
      searchDirectory: vi.fn(),
      createPrincipal: vi.fn(),
    },
  }
})

vi.mock('../api', () => ({
  ApiError: mocks.TestApiError,
  useMosaicApi: () => mocks.api,
}))

function renderPicker(onManualFallback = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onCreated = vi.fn()
  render(
    <QueryClientProvider client={queryClient}>
      <DirectoryPrincipalPicker onCreated={onCreated} onManualFallback={onManualFallback} />
    </QueryClientProvider>,
  )
  return { onCreated, onManualFallback }
}

describe('DirectoryPrincipalPicker', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.api.searchDirectory.mockResolvedValue([
      {
        objectId: 'user-object',
        kind: 'user',
        displayName: 'Ada Lovelace',
        detail: 'ada@example.com',
      },
      {
        objectId: 'agent-user-object',
        kind: 'agentUser',
        displayName: 'Agent runner',
        detail: 'agent@example.com',
        identityParentId: 'agent-parent',
        principalId: 'principal_existing',
      },
    ])
    mocks.api.createPrincipal.mockResolvedValue({
      id: 'principal_new',
      tenantId: 'tenant',
      objectId: 'user-object',
      kind: 'user',
      label: 'Ada Lovelace',
      createdAt: '',
      updatedAt: '',
    })
  })

  it('debounces search, shows results, creates a principal, and marks existing results', async () => {
    const user = userEvent.setup()
    const { onCreated } = renderPicker()

    await user.type(screen.getByRole('textbox', { name: 'Directory search' }), 'ad')
    expect(mocks.api.searchDirectory).not.toHaveBeenCalled()

    expect(await screen.findByText('Ada Lovelace')).toBeVisible()
    expect(screen.getByText('Already added')).toBeVisible()
    expect(screen.getByText('Parent agent agent-parent')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Select Ada Lovelace' }))
    expect(mocks.api.createPrincipal).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Add Ada Lovelace' }))

    await waitFor(() => expect(mocks.api.createPrincipal).toHaveBeenCalledWith({
      objectId: 'user-object',
      kind: 'user',
      label: 'Ada Lovelace',
    }))
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: 'principal_new' }))
  })

  it('searches the kind it is given and reports kind changes', async () => {
    const user = userEvent.setup()
    const onKindChange = vi.fn()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <DirectoryPrincipalPicker kind="agent" onKindChange={onKindChange} />
      </QueryClientProvider>,
    )

    expect(screen.getByRole('combobox', { name: 'Directory search kind' })).toHaveValue('agent')
    await user.type(screen.getByRole('textbox', { name: 'Directory search' }), 'bot')
    await waitFor(() => expect(mocks.api.searchDirectory).toHaveBeenCalledWith('agent', 'bot', 20))

    await user.selectOptions(screen.getByRole('combobox', { name: 'Directory search kind' }), 'group')
    expect(onKindChange).toHaveBeenCalledWith('group')
  })

  it.each([
    [new mocks.TestApiError('Directory lookup requires Entra authentication.', 409, { code: 'directory_disabled' }), 'Directory lookup requires Entra authentication. Use manual entry instead.'],
    [new mocks.TestApiError('Missing permission User.ReadBasic.All.', 403, { code: 'directory_forbidden' }), 'Missing permission User.ReadBasic.All.'],
    [new mocks.TestApiError('Graph unavailable.', 502, { code: 'directory_unavailable' }), "Couldn't reach Microsoft Graph. Try again."],
  ])('shows a friendly directory error (%#)', async (error, message) => {
    const user = userEvent.setup()
    const onManualFallback = vi.fn()
    mocks.api.searchDirectory.mockRejectedValue(error)
    renderPicker(onManualFallback)

    await user.type(screen.getByRole('textbox', { name: 'Directory search' }), 'ad')

    expect(await screen.findByText(message)).toBeVisible()
    expect(onManualFallback).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Use manual entry' }))
    expect(onManualFallback).toHaveBeenCalledOnce()
  })

  it('hides stale results immediately while a new query is being debounced', async () => {
    const user = userEvent.setup()
    renderPicker()
    const input = screen.getByRole('textbox', { name: 'Directory search' })
    await user.type(input, 'ad')
    await user.click(await screen.findByRole('button', { name: 'Select Ada Lovelace' }))
    expect(screen.getByRole('button', { name: 'Add Ada Lovelace' })).toBeVisible()
    await user.clear(input)
    expect(screen.queryByRole('button', { name: 'Add Ada Lovelace' })).not.toBeInTheDocument()
    await user.type(input, 'ad')
    await screen.findByRole('button', { name: 'Select Ada Lovelace' })
    expect(screen.queryByRole('button', { name: 'Add Ada Lovelace' })).not.toBeInTheDocument()
    expect(mocks.api.createPrincipal).not.toHaveBeenCalled()
  })

  it('explains an empty result and supports retry after a permission failure', async () => {
    const user = userEvent.setup()
    mocks.api.searchDirectory
      .mockRejectedValueOnce(new mocks.TestApiError('Missing Application.Read.All.', 403))
      .mockResolvedValue([])
    renderPicker()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Directory search kind' }), 'application')
    await user.type(screen.getByRole('textbox', { name: 'Directory search' }), 'app')
    expect(await screen.findByText('Missing Application.Read.All.')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Check directory configuration' })).toHaveAttribute('href', '/settings?tab=runtime')
    await user.click(screen.getByRole('button', { name: 'Retry directory search' }))
    expect(await screen.findByText(/No matching directory identities/)).toBeVisible()
    expect(mocks.api.searchDirectory).toHaveBeenLastCalledWith('application', 'app', 20)
  })

  it('disambiguates equal friendly names and registers the selected managed identity object, not its app ID', async () => {
    const user = userEvent.setup()
    mocks.api.searchDirectory.mockResolvedValue([
      { objectId: 'object-one', kind: 'servicePrincipal', displayName: 'Build', appId: 'app-one' },
      { objectId: 'object-two', kind: 'managedIdentity', displayName: 'Build', appId: 'app-two' },
    ])
    renderPicker()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Directory search kind' }), 'application')
    await user.type(screen.getByRole('textbox', { name: 'Directory search' }), 'bu')
    const choices = await screen.findAllByRole('button', { name: /^Select Build/ })
    expect(choices).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Select Build (Managed identity; app-two)' }))
    await user.click(screen.getByRole('button', { name: 'Add Build' }))
    await waitFor(() => expect(mocks.api.createPrincipal).toHaveBeenCalledWith({
      objectId: 'object-two', kind: 'managedIdentity', label: 'Build',
    }))
  })

  it('requires a fresh selection after changing search kinds and never submits a stale result', async () => {
    const user = userEvent.setup()
    renderPicker()
    await user.type(screen.getByRole('textbox', { name: 'Directory search' }), 'ad')
    await user.click(await screen.findByRole('button', { name: 'Select Ada Lovelace' }))
    expect(screen.getByRole('button', { name: 'Add Ada Lovelace' })).toBeVisible()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Directory search kind' }), 'agent')
    expect(screen.queryByRole('button', { name: 'Add Ada Lovelace' })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Directory search kind' }), 'user')
    expect(screen.queryByRole('button', { name: 'Add Ada Lovelace' })).not.toBeInTheDocument()
    expect(mocks.api.createPrincipal).not.toHaveBeenCalled()
  })
})
