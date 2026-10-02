import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { specsWithMemberDrained } from '../pools'
import {
  anthropicPool,
  anthropicPoolDetail,
  draftPool,
  observedGateway,
  poolGateway,
  poolPlan,
  poolRun,
} from '../test/pool-fixtures'
import type { ModelPoolDetail, PublishRun } from '../types'
import { PoolDetailPage } from './PoolDetailPage'

const api = {
  getEnvironmentCatalog: vi.fn(),
  getModelPoolDetail: vi.fn(),
  listModelPoolRuns: vi.fn(),
  listGateways: vi.fn(),
  updateModelPool: vi.fn(),
  deleteModelPool: vi.fn(),
  planModelPool: vi.fn(),
  planUnpublishModelPool: vi.fn(),
  getModelPoolLock: vi.fn(),
  recoverModelPool: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

const draftDetail: ModelPoolDetail = {
  ...anthropicPoolDetail,
  pool: draftPool,
  baseUrl: 'https://apim-contoso-ai.example.test/mosaic/pool-openai-chat',
  models: [],
  warnings: [],
}

type Entry = string | { pathname: string; state: unknown }

function renderPage(entry: Entry = `/pools/${anthropicPool.id}`) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <FluentProvider theme={webLightTheme}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/pools" element={<p>Pool list</p>} />
            <Route path="/pools/:poolId" element={<PoolDetailPage />} />
          </Routes>
        </MemoryRouter>
      </FluentProvider>
    </QueryClientProvider>,
  )
}

describe('PoolDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [],
      requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [],
      updatedAt: null,
    })
    api.getModelPoolDetail.mockResolvedValue(anthropicPoolDetail)
    api.listModelPoolRuns.mockResolvedValue([poolRun])
    api.listGateways.mockResolvedValue([poolGateway])
  })

  it('shows each model’s deployments, how the pool routes, and how callers connect', async () => {
    renderPage()

    expect(await screen.findByRole('heading', { name: 'Anthropic Claude', level: 1 })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Claude Opus 4.5', level: 3 })).toBeVisible()
    expect(screen.getByText('2 of 3 deployments active')).toBeVisible()

    const deployments = screen.getByRole('table', { name: 'Deployments serving Claude Opus 4.5' })
    const [, eastUs2, northCentral, westUs3] = within(deployments).getAllByRole('row')
    expect(within(eastUs2).getByText('67% of requests')).toBeVisible()
    expect(within(northCentral).getByText('33% of requests')).toBeVisible()
    expect(within(westUs3).getByText('No requests')).toBeVisible()
    expect(within(westUs3).getByRole('switch', { name: 'Drain claude-opus-4-5 on foundry-westus3' })).toBeChecked()
    expect(within(eastUs2).getByRole('switch', { name: 'Drain claude-opus-4-5 on foundry-eastus2' })).not.toBeChecked()

    expect(screen.getByText('Up to 3 retries, each on another deployment')).toBeVisible()
    expect(screen.getByText('200,000 tokens per minute, for each model')).toBeVisible()
    expect(screen.getByText(anthropicPoolDetail.baseUrl ?? '')).toBeVisible()
    expect(screen.getByText(/never see the deployments behind them/)).toBeVisible()
    expect(screen.getByText('Worth checking')).toBeVisible()

    const runs = await screen.findByRole('table', { name: 'Pool runs' })
    const [, latest] = within(runs).getAllByRole('row')
    expect(within(latest).getByText('Publish')).toBeVisible()
    expect(within(latest).getByText('20.0 s')).toBeVisible()
    expect(within(latest).getByText('1 change')).toBeVisible()

    expect(screen.getByRole('button', { name: 'Review plan' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Unpublish' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
  })

  it('saves a drained deployment and says the gateway follows once a plan is applied', async () => {
    const user = userEvent.setup()
    api.updateModelPool.mockResolvedValue(anthropicPool)
    renderPage()

    await user.click(await screen.findByRole('switch', { name: 'Drain claude-opus-4-5 on foundry-northcentralus' }))

    await waitFor(() => {
      expect(api.updateModelPool).toHaveBeenCalledWith(anthropicPool.id, {
        models: specsWithMemberDrained(
          anthropicPool,
          'poolmodel_opus',
          'endpoint_northcentralus',
          'claude-opus-4-5',
          true,
        ),
      })
    })
    expect(
      await screen.findByText(
        'claude-opus-4-5 on foundry-northcentralus is drained in the saved pool. The gateway keeps sending it requests until you review and apply a plan.',
      ),
    ).toBeVisible()
  })

  it('copies the base URL', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Copy base URL' }))

    expect(writeText).toHaveBeenCalledWith(anthropicPoolDetail.baseUrl)
    expect(await screen.findByRole('button', { name: 'Copied base URL' })).toBeVisible()
  })

  it('removes a pool that was never published and returns to the list', async () => {
    const user = userEvent.setup()
    api.getModelPoolDetail.mockResolvedValue(draftDetail)
    api.listModelPoolRuns.mockResolvedValue([])
    api.deleteModelPool.mockResolvedValue(undefined)
    renderPage(`/pools/${draftPool.id}`)

    expect(await screen.findByText('No runs yet. Publishing the pool records a run here.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Publish' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Unpublish' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Remove' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Remove OpenAI chat?' })
    expect(within(dialog).getByText(/never published, so nothing changes in API Management/)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Remove pool' }))

    expect(await screen.findByText('Pool list')).toBeVisible()
    expect(api.deleteModelPool).toHaveBeenCalledWith(draftPool.id)
  })

  it('opens the publish plan when the list saved the pool for review', async () => {
    api.getModelPoolDetail.mockResolvedValue(draftDetail)
    api.listModelPoolRuns.mockResolvedValue([])
    api.planModelPool.mockResolvedValue({ ...poolPlan, publicationId: draftPool.id })
    renderPage({ pathname: `/pools/${draftPool.id}`, state: { openPlan: true } })

    expect(await screen.findByRole('dialog', { name: 'Publish OpenAI chat' })).toBeVisible()
    expect(api.planModelPool).toHaveBeenCalledWith(draftPool.id)
  })

  it('blocks plans while MOSAIC only observes the gateway', async () => {
    api.listGateways.mockResolvedValue([{ ...observedGateway, id: poolGateway.id }])
    renderPage()

    expect(await screen.findByText('MOSAIC can’t change this pool in API Management')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Review plan' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Unpublish' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled()
  })

  it('asks for fixes before any plan', async () => {
    api.getModelPoolDetail.mockResolvedValue({
      ...anthropicPoolDetail,
      problems: ['Claude Opus 4.5 has no active deployments.'],
      unappliedChanges: true,
    })
    renderPage()

    expect(await screen.findByText('Fix these before you apply changes')).toBeVisible()
    expect(screen.getByText('Claude Opus 4.5 has no active deployments.')).toBeVisible()
    for (const button of screen.getAllByRole('button', { name: 'Review plan' })) {
      expect(button).toBeDisabled()
    }
  })

  it('says when saved changes aren’t running, and reviews a plan for them', async () => {
    const user = userEvent.setup()
    api.getModelPoolDetail.mockResolvedValue({ ...anthropicPoolDetail, unappliedChanges: true })
    api.planModelPool.mockResolvedValue(poolPlan)
    renderPage()

    expect(await screen.findByText('Saved changes aren’t running yet')).toBeVisible()
    const [, fromNotice] = screen.getAllByRole('button', { name: 'Review plan' })
    await user.click(fromNotice)

    expect(await screen.findByRole('dialog', { name: 'Publish Anthropic Claude' })).toBeVisible()
    expect(api.planModelPool).toHaveBeenCalledWith(anthropicPool.id)
  })

  it('offers recovery when the last run was interrupted', async () => {
    const interrupted: PublishRun = {
      ...poolRun,
      id: anthropicPool.lastRunId ?? '',
      status: 'interrupted',
      completedAt: null,
      durationMs: null,
    }
    api.listModelPoolRuns.mockResolvedValue([interrupted])
    renderPage()

    expect(await screen.findByText('The last run was interrupted. The gateway’s state is unknown.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Check recovery status (diagnostic only)' })).toBeVisible()
  })

  it('keeps the way back when the pool doesn’t load', async () => {
    api.getModelPoolDetail.mockRejectedValue(new Error('Pool not found.'))
    renderPage()

    expect(await screen.findByText('Unable to load the pool')).toBeVisible()
    expect(screen.getByRole('link', { name: '← Back to pools' })).toHaveAttribute('href', '/pools')
  })
})
