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
    await user.click(screen.getByRole('button', { name: 'Add Ada Lovelace' }))

    await waitFor(() => expect(mocks.api.createPrincipal).toHaveBeenCalledWith({
      objectId: 'user-object',
      kind: 'user',
      label: 'Ada Lovelace',
    }))
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: 'principal_new' }))
  })

  it.each([
    [new mocks.TestApiError('Directory search is off.', 409, { code: 'directory_disabled' }), 'Directory search is off. Use manual entry instead.'],
    [new mocks.TestApiError('Missing permission User.ReadBasic.All.', 403, { code: 'directory_forbidden' }), 'Missing permission User.ReadBasic.All.'],
    [new mocks.TestApiError('Graph unavailable.', 502, { code: 'directory_unavailable' }), "Couldn't reach Microsoft Graph. Try again."],
  ])('shows a friendly directory error (%#)', async (error, message) => {
    const user = userEvent.setup()
    const onManualFallback = vi.fn()
    mocks.api.searchDirectory.mockRejectedValue(error)
    renderPicker(onManualFallback)

    await user.type(screen.getByRole('textbox', { name: 'Directory search' }), 'ad')

    expect(await screen.findByText(message)).toBeVisible()
    if (error.status === 409) {
      expect(onManualFallback).toHaveBeenCalled()
    }
  })
})
