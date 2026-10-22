import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DirectoryStatus, DirectoryVerificationStatus } from '../types'
import { DirectoryGroupVerification } from './DirectoryGroupVerification'

const api = vi.hoisted(() => ({
  getDirectoryStatus: vi.fn(),
  checkDirectoryGroup: vi.fn(),
}))
vi.mock('../api', () => ({ useMosaicApi: () => api }))

const GROUP = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const OTHER_GROUP = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
let server: DirectoryStatus

function ready(groupObjectId = GROUP): DirectoryVerificationStatus {
  const checkedAt = new Date(Date.now() - 1000).toISOString()
  return {
    state: 'ready',
    groupObjectId,
    checkedAt,
    completedAt: new Date().toISOString(),
    lastSuccessfulAt: checkedAt,
    lastSuccessfulGroupObjectId: groupObjectId,
    freshUntil: new Date(Date.now() + 300_000).toISOString(),
  }
}

function mount(groupObjectId?: string, client = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})) {
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter><DirectoryGroupVerification groupObjectId={groupObjectId} /></MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...view, client }
}

async function advance(milliseconds = 1) {
  await act(() => vi.advanceTimersByTimeAsync(milliseconds))
}

describe('DirectoryGroupVerification', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    server = {
      lookupEnabled: true,
      groupClaimsEnabled: true,
      permissions: [],
      membershipCheck: { state: 'notChecked' },
    }
    api.getDirectoryStatus.mockImplementation(async () => server)
    api.checkDirectoryGroup.mockImplementation(async (groupObjectId: string) => {
      const result = ready(groupObjectId)
      server = { ...server, membershipCheck: result }
      return result
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('never probes on mount or refresh and verifies only the explicitly selected group', async () => {
    const user = userEvent.setup()
    mount(GROUP)
    expect(await screen.findByText('Not checked', { exact: true })).toBeVisible()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Refresh group-check status' }))
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Verify complete group read' }))

    expect(await screen.findByText('Verified', { exact: true })).toBeVisible()
    expect(api.checkDirectoryGroup).toHaveBeenCalledExactlyOnceWith(GROUP)
    expect(api.getDirectoryStatus.mock.calls.every(args => args.length === 0)).toBe(true)
    expect(screen.getByText(/does not verify other groups or grant model access/)).toBeVisible()
    expect(screen.getByText(/process-local, can differ between API instances/)).toBeVisible()
  })

  it('offers Settings a real Identity link rather than choosing or probing a group automatically', async () => {
    server.membershipCheck = ready()
    mount()

    expect(await screen.findByText('Verified', { exact: true })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Choose a group in Identity to verify' }))
      .toHaveAttribute('href', '/identity?tab=security-groups')
    expect(screen.queryByRole('button', { name: 'Verify complete group read' })).not.toBeInTheDocument()
    expect(screen.getByText(GROUP)).toBeVisible()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('does not claim another group observation verifies the selected group', async () => {
    server.membershipCheck = ready(OTHER_GROUP)
    mount(GROUP)

    expect(await screen.findByText('Other group: Verified', { exact: true })).toBeVisible()
    expect(screen.getByText('That observation does not verify the selected group.')).toBeVisible()
    expect(screen.getByText(OTHER_GROUP)).toBeVisible()
    expect(screen.queryByText('Verified', { exact: true })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Verify complete group read' })).toBeEnabled()
  })

  it('updates shared observations without replacing the separately checked consent snapshot', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const permission = {
      permission: 'GroupMember.Read.All', capability: 'Groups', required: true, granted: true,
    }
    client.setQueryData(['directory', 'status', 'permissions'], { ...server, permissions: [permission] })
    const user = userEvent.setup()
    mount(GROUP, client)
    await screen.findByText('Not checked', { exact: true })
    await user.click(screen.getByRole('button', { name: 'Verify complete group read' }))
    await screen.findByText('Verified', { exact: true })

    const cached = client.getQueryData<DirectoryStatus>(['directory', 'status', 'permissions'])
    expect(cached?.permissions).toEqual([permission])
    expect(cached?.membershipCheck).toEqual(server.membershipCheck)
  })

  it('surfaces a failed read and keeps previous success associated with its original group', async () => {
    const previous = ready(OTHER_GROUP)
    api.checkDirectoryGroup.mockImplementation(async () => {
      server = { ...server, membershipCheck: {
        state: 'forbidden',
        groupObjectId: GROUP,
        checkedAt: new Date().toISOString(),
        lastSuccessfulAt: previous.lastSuccessfulAt,
        lastSuccessfulGroupObjectId: OTHER_GROUP,
        message: 'Group scope is not readable',
      } }
      throw new Error('Group scope is not readable')
    })
    const user = userEvent.setup()
    mount(GROUP)
    await screen.findByText('Not checked', { exact: true })
    await user.click(screen.getByRole('button', { name: 'Verify complete group read' }))

    expect(await screen.findByText('Group verification failed')).toBeVisible()
    expect(await screen.findByText('Permission denied', { exact: true })).toBeVisible()
    expect(screen.getAllByText('Group scope is not readable')).toHaveLength(1)
    expect(screen.getByText('Last successful group object ID')).toBeVisible()
    expect(screen.getByText(OTHER_GROUP)).toBeVisible()
    expect(screen.queryByText('Verified', { exact: true })).not.toBeInTheDocument()
    expect(api.checkDirectoryGroup).toHaveBeenCalledTimes(1)
  })

  it('shows persisted failures even when no check was started in this view', async () => {
    server.membershipCheck = {
      state: 'unavailable', groupObjectId: GROUP, message: 'The last member page failed',
    }
    mount(GROUP)

    expect(await screen.findByText('Read failed', { exact: true })).toBeVisible()
    expect(screen.getByText('The last member page failed')).toBeVisible()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('polls accepted progress with status reads and never repeats the verification', async () => {
    vi.useFakeTimers()
    api.checkDirectoryGroup.mockImplementation(async () => {
      const progress: DirectoryVerificationStatus = { state: 'checking', groupObjectId: GROUP }
      server = { ...server, membershipCheck: progress }
      return progress
    })
    mount(GROUP)
    await advance()
    fireEvent.click(screen.getByRole('button', { name: 'Verify complete group read' }))
    await advance()
    expect(screen.getByText('Checking', { exact: true })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Verify complete group read' })).toBeDisabled()

    server = { ...server, membershipCheck: ready() }
    await advance(2100)

    expect(screen.getByText('Verified', { exact: true })).toBeVisible()
    expect(api.getDirectoryStatus.mock.calls.length).toBeGreaterThan(2)
    expect(api.checkDirectoryGroup).toHaveBeenCalledTimes(1)
    const reads = api.getDirectoryStatus.mock.calls.length
    await advance(5000)
    expect(api.getDirectoryStatus).toHaveBeenCalledTimes(reads)
  })

  it('expires a displayed success without renewing the observation or starting another read', async () => {
    vi.useFakeTimers()
    server.membershipCheck = { ...ready(), freshUntil: new Date(Date.now() + 1500).toISOString() }
    const original = { ...server.membershipCheck }
    mount(GROUP)
    await advance()
    expect(screen.getByText('Verified', { exact: true })).toBeVisible()

    await advance(1700)

    expect(screen.getByText('Stale', { exact: true })).toBeVisible()
    expect(server.membershipCheck).toEqual(original)
    expect(api.getDirectoryStatus).toHaveBeenCalledTimes(2)
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('respects the shared cooldown and enables a manual retry only after it elapses', async () => {
    vi.useFakeTimers()
    server.membershipCheck = {
      state: 'throttled',
      groupObjectId: OTHER_GROUP,
      retryAt: new Date(Date.now() + 1800).toISOString(),
      message: 'Wait before retrying Graph',
    }
    mount(GROUP)
    await advance()
    const button = screen.getByRole('button', { name: 'Verify complete group read' })
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()

    await advance(2100)

    expect(button).toBeEnabled()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
    expect(screen.getByText('Retry after')).toBeVisible()
  })

  it.each(['expired', 'future', 'missing', 'malformed'])(
    'never displays %s observations as current success',
    async (time) => {
      server.membershipCheck = {
        ...ready(),
        ...(time === 'expired' ? { freshUntil: new Date(Date.now() - 1).toISOString() }
          : time === 'future' ? { checkedAt: new Date(Date.now() + 60_000).toISOString() }
            : time === 'missing' ? { freshUntil: null } : { checkedAt: 'not-a-date' }),
      }
      mount(GROUP)

      expect(await screen.findByText('Stale', { exact: true })).toBeVisible()
      expect(screen.queryByText('Verified', { exact: true })).not.toBeInTheDocument()
      expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
    },
  )

  it('does not offer verification without an integration', async () => {
    server = { ...server, lookupEnabled: false, membershipCheck: { state: 'disabled' } }
    mount(GROUP)

    expect(await screen.findByText('Unavailable', { exact: true })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Verify complete group read' })).toBeDisabled()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('keeps status errors visible instead of reporting a disabled or healthy integration', async () => {
    api.getDirectoryStatus.mockRejectedValue(new Error('Status could not be read'))
    const user = userEvent.setup()
    mount(GROUP)
    expect(await screen.findByText('Group verification status unavailable')).toBeVisible()
    expect(screen.getByText('Status could not be read')).toBeVisible()
    expect(screen.queryByText('Unavailable', { exact: true })).not.toBeInTheDocument()
    expect(screen.queryByText('Verified', { exact: true })).not.toBeInTheDocument()

    api.getDirectoryStatus.mockImplementation(async () => server)
    await user.click(screen.getByRole('button', { name: 'Refresh group-check status' }))
    expect(await screen.findByText('Not checked', { exact: true })).toBeVisible()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('does not interpret an older API response as functional success', async () => {
    delete server.membershipCheck
    mount(GROUP)

    expect(await screen.findByText('Not reported', { exact: true })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Verify complete group read' })).toBeDisabled()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('does not retain a success badge when refreshing the shared observation fails', async () => {
    server.membershipCheck = ready()
    const user = userEvent.setup()
    mount(GROUP)
    await screen.findByText('Verified', { exact: true })
    api.getDirectoryStatus.mockRejectedValue(new Error('Refresh failed'))

    await user.click(screen.getByRole('button', { name: 'Refresh group-check status' }))

    expect(await screen.findByText('Group verification status unavailable')).toBeVisible()
    expect(screen.queryByText('Verified', { exact: true })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Verify complete group read' })).toBeDisabled()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('disables another group check while a shared complete read is in progress', async () => {
    server.membershipCheck = { state: 'checking', groupObjectId: OTHER_GROUP }
    mount(GROUP)

    expect(await screen.findByText('Other group: Checking', { exact: true })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Verify complete group read' })).toBeDisabled()
    expect(api.checkDirectoryGroup).not.toHaveBeenCalled()
  })

  it('allows an explicit check after cancellation without treating cancellation as success', async () => {
    server.membershipCheck = { state: 'cancelled', groupObjectId: GROUP, message: 'Read was cancelled' }
    mount(GROUP)

    expect(await screen.findByText('Cancelled', { exact: true })).toBeVisible()
    expect(screen.getByText('Read was cancelled')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Verify complete group read' })).toBeEnabled()
  })
})
