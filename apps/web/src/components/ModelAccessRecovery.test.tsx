import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ModelAccessRecovery } from './ModelAccessRecovery'

const api = {
  getPublicationLock: vi.fn(),
  diagnosePublicationRecovery: vi.fn(),
  applyPublishPlan: vi.fn(),
}
vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderRecovery(runId: string | null = 'run_1') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ModelAccessRecovery publicationId="pub_1" runId={runId} />
    </QueryClientProvider>,
  )
  return queryClient
}

describe('ModelAccessRecovery', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.getPublicationLock.mockResolvedValue({ publicationId: 'pub_1', ownerId: 'run_1' })
    api.diagnosePublicationRecovery.mockResolvedValue({
      id: 'run_1', status: 'interrupted', errors: ['Original worker may still own ARM operations.'],
    })
  })

  it('explains retained-lock prerequisites and only diagnoses on explicit request', async () => {
    const user = userEvent.setup()
    const queryClient = renderRecovery()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    expect(screen.getByText(/apply lock may still be retained/)).toBeVisible()
    expect(screen.getByText(/verify that all submitted ARM operations are terminal/)).toBeVisible()
    expect(api.diagnosePublicationRecovery).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Check recovery status (diagnostic only)' }))
    expect(api.diagnosePublicationRecovery).toHaveBeenCalledWith('pub_1', 'run_1')
    expect(await screen.findByText('Diagnostic reported run status: interrupted')).toBeVisible()
    expect(screen.getByText(/An interrupted run status alone does not establish recovery or lock release/)).toBeVisible()
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['publications'] })
    expect(screen.getByText(/No operator confirmation was sent/)).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /confirm recovery/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('shows diagnostic failure without claiming recovery or retrying apply', async () => {
    const user = userEvent.setup()
    api.diagnosePublicationRecovery.mockRejectedValue(new Error('The run cannot be diagnosed while another recovery is active.'))
    renderRecovery()
    await user.click(screen.getByRole('button', { name: 'Check recovery status (diagnostic only)' }))
    expect(await screen.findByText('The run cannot be diagnosed while another recovery is active.')).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    expect(screen.queryByText(/Diagnostic reported run status/)).not.toBeInTheDocument()
  })

  it('discovers retained mutation locks even when no publish run exists', async () => {
    const user = userEvent.setup()
    api.getPublicationLock.mockResolvedValue({ publicationId: 'pub_1', ownerId: 'mutation_1' })
    renderRecovery(null)
    expect(screen.getByText(/This UI never confirms quiescence/)).toBeVisible()
    expect(api.diagnosePublicationRecovery).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Check recovery status (diagnostic only)' }))
    expect(api.getPublicationLock).toHaveBeenCalledWith('pub_1')
    expect(api.diagnosePublicationRecovery).toHaveBeenCalledWith('pub_1', 'mutation_1')
  })

  it('does not diagnose or recover an old run once its lock is gone', async () => {
    const user = userEvent.setup()
    api.getPublicationLock.mockResolvedValue({ publicationId: 'pub_1', ownerId: null })
    renderRecovery()
    await user.click(screen.getByRole('button', { name: 'Check recovery status (diagnostic only)' }))
    expect(await screen.findByText('No publication write lock is currently held.')).toBeVisible()
    expect(api.diagnosePublicationRecovery).not.toHaveBeenCalled()
  })
})
