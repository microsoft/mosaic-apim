import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { anthropicPool, governedAnthropicPool, poolPlan, poolRun } from '../test/pool-fixtures'
import type { PoolAccessSnapshot, PublishPlan, PublishRun } from '../types'
import { PoolPlanDialog } from './PoolPlanDialog'

const api = {
  planModelPool: vi.fn(),
  planUnpublishModelPool: vi.fn(),
  applyModelPool: vi.fn(),
  unpublishModelPool: vi.fn(),
  getModelPoolRun: vi.fn(),
  getModelPoolLock: vi.fn(),
  recoverModelPool: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

const running: PublishRun = { ...poolRun, status: 'running', completedAt: null, durationMs: null, steps: [] }

const governedAccess = governedAnthropicPool.appliedAccess as PoolAccessSnapshot

const unpublishPlan: PublishPlan = {
  ...poolPlan,
  id: 'publishplan_anthropic_unpublish',
  operation: 'unpublish',
  steps: [
    {
      kind: 'api',
      name: 'mosaic-pool-anthropic-claude',
      action: 'delete',
      reason: 'MOSAIC created it.',
      resourceId: '/apis/mosaic-pool-anthropic-claude',
      existed: true,
    },
    {
      kind: 'backendPool',
      name: 'mosaic-pool-anthropic-claude-opus',
      action: 'delete',
      reason: 'MOSAIC created it.',
      resourceId: '/backends/mosaic-pool-anthropic-claude-opus',
      existed: true,
    },
  ],
}

function renderDialog(mode: 'publish' | 'unpublish' = 'publish') {
  const onClose = vi.fn()
  const onFinished = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <FluentProvider theme={webLightTheme}>
        <MemoryRouter>
          <PoolPlanDialog pool={anthropicPool} mode={mode} onClose={onClose} onFinished={onFinished} />
        </MemoryRouter>
      </FluentProvider>
    </QueryClientProvider>,
  )
  return { onClose, onFinished }
}

describe('PoolPlanDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.planModelPool.mockResolvedValue(poolPlan)
    api.planUnpublishModelPool.mockResolvedValue(unpublishPlan)
    api.applyModelPool.mockResolvedValue(running)
    api.getModelPoolRun.mockResolvedValue(poolRun)
  })

  it('applies the plan the administrator reviewed, then reports the run', async () => {
    const user = userEvent.setup()
    const { onFinished } = renderDialog()
    const dialog = await screen.findByRole('dialog', { name: 'Publish Anthropic Claude' })

    const review = await within(dialog).findByRole('region', { name: 'Plan review' })
    await waitFor(() => expect(review).toHaveFocus())
    expect(within(review).getByText('1 change across 2 resources, written in this order.')).toBeVisible()
    const [, first, second] = within(review).getAllByRole('row')
    expect(within(first).getByText('Backend pool')).toBeVisible()
    expect(within(first).getByText('mosaic-pool-anthropic-claude-opus')).toBeVisible()
    expect(within(first).getByText('Update')).toBeVisible()
    expect(within(first).getByText('A member was drained.')).toBeVisible()
    expect(within(second).getByText('No change')).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Apply plan' }))

    expect(await within(dialog).findByText('Apply finished')).toBeVisible()
    expect(api.applyModelPool).toHaveBeenCalledWith(anthropicPool.id, poolPlan.id)
    expect(api.getModelPoolRun).toHaveBeenCalledWith(anthropicPool.id, poolRun.id)
    expect(within(dialog).getByRole('table', { name: 'Apply run steps' })).toBeVisible()
    expect(
      within(dialog).getByText(
        'The local development service reported completion. Live API Management changes aren’t verified.',
      ),
    ).toBeVisible()
    await waitFor(() => expect(onFinished).toHaveBeenCalledWith(poolRun))
    expect(onFinished).toHaveBeenCalledTimes(1)
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeEnabled()
    expect(within(dialog).queryByRole('button', { name: 'Apply plan' })).not.toBeInTheDocument()
  })

  it('asks before it unpublishes, and lists what it deletes', async () => {
    const user = userEvent.setup()
    api.unpublishModelPool.mockResolvedValue({
      ...poolRun,
      id: 'publishrun_anthropic_unpublish',
      planId: unpublishPlan.id,
      steps: [],
    })
    renderDialog('unpublish')
    const dialog = await screen.findByRole('alertdialog', { name: 'Unpublish Anthropic Claude?' })

    expect(await within(dialog).findByText('What MOSAIC deletes')).toBeVisible()
    expect(
      within(dialog).getByText(
        '2 resources MOSAIC created, deleted in this order. MOSAIC leaves everything else in API Management alone.',
      ),
    ).toBeVisible()
    expect(within(dialog).getByRole('table', { name: 'Unpublish plan steps' })).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Unpublish pool' }))

    expect(await within(dialog).findByText('Unpublish finished')).toBeVisible()
    expect(api.unpublishModelPool).toHaveBeenCalledWith(anthropicPool.id, unpublishPlan.id)
    expect(api.applyModelPool).not.toHaveBeenCalled()
  })

  it('lists what to fix when the pool can’t be planned', async () => {
    api.planModelPool.mockRejectedValue(
      new ApiError('The pool has problems to fix.', 409, {
        details: { problems: ['Claude Opus 4.5: choose at least one deployment.'] },
      }),
    )
    renderDialog()
    const dialog = await screen.findByRole('dialog', { name: 'Publish Anthropic Claude' })

    expect(await within(dialog).findByText('Fix these before you publish Anthropic Claude')).toBeVisible()
    expect(within(dialog).getByText('Claude Opus 4.5: choose at least one deployment.')).toBeVisible()
    expect(within(dialog).queryByRole('button', { name: 'Apply plan' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeEnabled()
  })

  it('plans again when MOSAIC refuses the plan that was reviewed', async () => {
    const user = userEvent.setup()
    const fresh: PublishPlan = { ...poolPlan, id: 'publishplan_anthropic_3', digest: 'digest-anthropic-3' }
    api.planModelPool.mockResolvedValueOnce(poolPlan).mockResolvedValueOnce(fresh)
    api.applyModelPool.mockRejectedValueOnce(
      new ApiError('The pool changed after this plan was made. Review a new plan.', 409),
    )
    renderDialog()
    const dialog = await screen.findByRole('dialog', { name: 'Publish Anthropic Claude' })

    await user.click(await within(dialog).findByRole('button', { name: 'Apply plan' }))

    expect(await within(dialog).findByText('MOSAIC didn’t run the plan you reviewed')).toBeVisible()
    expect(within(dialog).getByText('The pool changed after this plan was made. Review a new plan.')).toBeVisible()
    expect(
      await within(dialog).findByText('MOSAIC has planned again. Review the fresh plan below before you continue.'),
    ).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Apply plan' }))

    await waitFor(() => expect(api.applyModelPool).toHaveBeenLastCalledWith(anthropicPool.id, fresh.id))
    expect(api.planModelPool).toHaveBeenCalledTimes(2)
  })

  it('has nothing to apply when the gateway already matches', async () => {
    api.planModelPool.mockResolvedValue({ ...poolPlan, steps: [poolPlan.steps[1]] })
    renderDialog()
    const dialog = await screen.findByRole('dialog', { name: 'Publish Anthropic Claude' })

    expect(
      await within(dialog).findByText('API Management already matches Anthropic Claude. There’s nothing to apply.'),
    ).toBeVisible()
    expect(within(dialog).getByText('0 changes across 1 resource, written in this order.')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Apply plan' })).toBeDisabled()
  })

  it('offers recovery when a run is interrupted', async () => {
    const user = userEvent.setup()
    api.getModelPoolRun.mockResolvedValue({ ...poolRun, status: 'interrupted' })
    renderDialog()
    const dialog = await screen.findByRole('dialog', { name: 'Publish Anthropic Claude' })

    await user.click(await within(dialog).findByRole('button', { name: 'Apply plan' }))

    expect(await within(dialog).findByText('Apply interrupted. The gateway’s state is unknown.')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Check recovery status (diagnostic only)' })).toBeVisible()
    expect(within(dialog).getByText(/The pool's apply lock may still be retained/)).toBeVisible()
  })

  it('reviews every grant the first governed apply enforces, and says the shared subscription stops working', async () => {
    api.planModelPool.mockResolvedValue({ ...poolPlan, poolAccessSnapshot: governedAccess, previousAccessVersion: null })
    renderDialog()
    const dialog = await screen.findByRole('dialog', { name: 'Publish Anthropic Claude' })

    const access = await within(dialog).findByRole('region', { name: 'Pool access review' })
    expect(within(access).getByRole('heading', { name: 'Who can call the pool' })).toBeVisible()
    expect(within(access).getByText('The shared subscription stops working')).toBeVisible()
    expect(within(access).getByText('Subscription key OR Entra token')).toBeVisible()
    expect(within(access).getByText('none → 2')).toBeVisible()

    const grants = within(access).getByRole('table', { name: 'Pool grants' })
    const [, megan, research] = within(grants).getAllByRole('row')
    expect(within(megan).getByText('Claude Opus 4.5')).toBeVisible()
    expect(within(megan).getByText('Megan Bowen')).toBeVisible()
    expect(within(megan).getByText('mosaic-pool-anthropic-claude-megan')).toBeVisible()
    expect(within(megan).getByText('Limits usage to 20,000 tokens per minute.')).toBeVisible()
    expect(within(megan).getByText('Allowed')).toBeVisible()
    expect(within(research).getByText('Research engineers')).toBeVisible()
    expect(within(research).getByText('None (Entra token)')).toBeVisible()
    expect(
      within(access).getByText(
        'Claude Opus 4.5, FIN-001: 5,000,000 tokens a month, shared by everyone the cost center grants it to',
      ),
    ).toBeVisible()
  })

  it('doesn’t warn about the shared subscription once the pool already governs access', async () => {
    api.planModelPool.mockResolvedValue({
      ...poolPlan,
      poolAccessSnapshot: { ...governedAccess, version: 3, grants: [] },
      previousAccessVersion: 2,
    })
    renderDialog()

    const access = await screen.findByRole('region', { name: 'Pool access review' })
    expect(within(access).queryByText('The shared subscription stops working')).not.toBeInTheDocument()
    expect(within(access).getByText('2 → 3')).toBeVisible()
    expect(within(access).getByText('The target has no grants, so the gateway refuses every caller.')).toBeVisible()
  })
})
