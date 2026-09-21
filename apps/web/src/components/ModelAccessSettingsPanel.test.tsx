import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { modelPublication, publishedModelApi } from '../test/model-access'
import type { Publication } from '../types'
import { ModelAccessSettingsPanel } from './ModelAccessSettingsPanel'

const api = {
  updatePublication: vi.fn(),
  linkPublicationModelApi: vi.fn(),
  applyPublishPlan: vi.fn(),
}
vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderPanel(overrides: Partial<Publication> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ModelAccessSettingsPanel
        publication={{ ...modelPublication, ...overrides }}
        reviewing={false}
        onReview={vi.fn()}
        onAddGrant={vi.fn()}
        onMessage={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

describe('ModelAccessSettingsPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.updatePublication.mockResolvedValue(modelPublication)
    api.linkPublicationModelApi.mockResolvedValue(publishedModelApi)
  })

  it('keeps a legacy publication unchanged until an explicit save and starts with both methods', async () => {
    const user = userEvent.setup()
    renderPanel({ governedAccess: null, appliedAccess: null })
    expect(screen.getByRole('switch', { name: 'Dedicated subscription keys' })).toBeChecked()
    expect(screen.getByRole('switch', { name: 'Microsoft Entra bearer tokens' })).toBeChecked()
    expect(screen.getByText(/generic bootstrap key/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Review model changes' })).toBeDisabled()
    expect(api.updatePublication).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Save access settings' }))
    await waitFor(() => expect(api.updatePublication).toHaveBeenCalledWith(modelPublication.id, {
      governedAccess: { keysEnabled: true, entraEnabled: true },
    }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it.each([
    ['Dedicated subscription keys', false, true, 'Entra token only'],
    ['Microsoft Entra bearer tokens', true, false, 'Subscription key only'],
  ] as const)('can disable %s independently without changing the last applied methods', async (label, keysEnabled, entraEnabled, summary) => {
    const user = userEvent.setup()
    renderPanel()
    await user.click(screen.getByRole('switch', { name: label }))
    expect(screen.getByText(summary)).toBeVisible()
    expect(screen.getByText('Last applied methods: Subscription key OR Entra token')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Review model changes' })).toBeDisabled()
    expect(api.updatePublication).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Save access settings' }))
    await waitFor(() => expect(api.updatePublication).toHaveBeenCalledWith(modelPublication.id, {
      governedAccess: { keysEnabled, entraEnabled },
    }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('allows disabling both methods as deny-all, not anonymous access', async () => {
    const user = userEvent.setup()
    renderPanel()
    await user.click(screen.getByRole('switch', { name: 'Dedicated subscription keys' }))
    await user.click(screen.getByRole('switch', { name: 'Microsoft Entra bearer tokens' }))
    expect(screen.getByText('Deny all — both methods disabled')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Save access settings' }))
    await waitFor(() => expect(api.updatePublication).toHaveBeenCalledWith(modelPublication.id, {
      governedAccess: { keysEnabled: false, entraEnabled: false },
    }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('links a missing canonical model only on an explicit action', async () => {
    const user = userEvent.setup()
    renderPanel({ modelApiId: null, governedAccess: null, appliedAccess: null })
    expect(api.linkPublicationModelApi).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Add direct grant' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Link published model' }))
    expect(api.linkPublicationModelApi).toHaveBeenCalledWith(modelPublication.id)
    expect(api.updatePublication).not.toHaveBeenCalled()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('reports a failed settings save and retains the earlier applied configuration', async () => {
    const user = userEvent.setup()
    api.updatePublication.mockRejectedValue(new Error('An apply is already in progress.'))
    renderPanel()
    await user.click(screen.getByRole('switch', { name: 'Dedicated subscription keys' }))
    await user.click(screen.getByRole('button', { name: 'Save access settings' }))
    expect(await screen.findByText('An apply is already in progress.')).toBeVisible()
    expect(screen.getByText('Last applied methods: Subscription key OR Entra token')).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('blocks model changes while runtime is unknown and explains retained-lock recovery', () => {
    renderPanel({ accessState: 'unknown' })
    expect(screen.getByText(/apply lock may still be retained/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Review model changes' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save access settings' })).toBeDisabled()
    expect(screen.getByRole('switch', { name: 'Dedicated subscription keys' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Check recovery status (diagnostic only)' })).toBeEnabled()
    expect(api.updatePublication).not.toHaveBeenCalled()
  })
})
