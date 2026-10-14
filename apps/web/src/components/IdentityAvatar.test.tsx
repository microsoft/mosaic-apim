import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IdentityAvatar } from './IdentityAvatar'

const mocks = vi.hoisted(() => ({
  getDirectoryPhoto: vi.fn(),
  accounts: [{ tenantId: 'tenant-a', homeAccountId: 'account-a' }],
  createObjectURL: vi.fn(),
  revokeObjectURL: vi.fn(),
}))
vi.mock('../api', () => ({ useMosaicApi: () => ({ getDirectoryPhoto: mocks.getDirectoryPhoto }) }))
vi.mock('@azure/msal-react', () => ({ useMsal: () => ({ accounts: mocks.accounts }) }))
const objectId = '00000000-0000-0000-0000-000000000001'

function renderAvatar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><IdentityAvatar name="Ada Lovelace" objectId={objectId} /></QueryClientProvider>)
}

describe('IdentityAvatar', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', undefined)
    vi.clearAllMocks()
    mocks.accounts = [{ tenantId: 'tenant-a', homeAccountId: 'account-a' }]
    mocks.getDirectoryPhoto.mockResolvedValue(null)
    mocks.createObjectURL.mockReturnValue('blob:photo')
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: mocks.createObjectURL })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: mocks.revokeObjectURL })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('uses initials for absent photos', async () => {
    const { container } = renderAvatar()
    await waitFor(() => expect(mocks.getDirectoryPhoto).toHaveBeenCalledWith(objectId))
    expect(container.textContent).toBe('AL')
    expect(container.querySelector('img')).toBeNull()
  })

  it('loads an authenticated blob and releases its URL on unmount', async () => {
    mocks.getDirectoryPhoto.mockResolvedValue(new Blob(['image'], { type: 'image/png' }))
    const { container, unmount } = renderAvatar()
    await waitFor(() => expect(container.querySelector('img')).toHaveAttribute('src', 'blob:photo'))
    unmount()
    expect(mocks.revokeObjectURL).toHaveBeenCalledWith('blob:photo')
  })

  it('retains a stable fallback and exposes photo failures without blocking', async () => {
    mocks.getDirectoryPhoto.mockRejectedValue(new Error('Photo permission missing'))
    const { container } = renderAvatar()
    await waitFor(() => expect(container.querySelector('span')).toHaveAttribute('data-photo-state', 'unavailable'))
    expect(container.textContent).toBe('AL')
    expect(mocks.getDirectoryPhoto).toHaveBeenCalledTimes(1)
  })

  it('uses workload icons without attempting a photo or requiring a query provider', () => {
    const { container } = render(<IdentityAvatar name="Support agent" kind="agentIdentity" objectId={objectId} />)
    expect(container.querySelector('svg')).not.toBeNull()
    expect(mocks.getDirectoryPhoto).not.toHaveBeenCalled()
  })

  it('does not reuse a photo across signed-in accounts', async () => {
    mocks.getDirectoryPhoto.mockResolvedValue(new Blob(['image'], { type: 'image/png' }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = () => <QueryClientProvider client={client}><IdentityAvatar name="Ada Lovelace" objectId={objectId} /></QueryClientProvider>
    const { rerender } = render(tree())
    await waitFor(() => expect(mocks.getDirectoryPhoto).toHaveBeenCalledTimes(1))
    mocks.accounts = [{ tenantId: 'tenant-b', homeAccountId: 'account-b' }]
    rerender(tree())
    await waitFor(() => expect(mocks.getDirectoryPhoto).toHaveBeenCalledTimes(2))
    expect(client.getQueryCache().getAll()).toHaveLength(2)
  })
})
