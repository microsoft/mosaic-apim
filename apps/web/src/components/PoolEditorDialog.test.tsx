import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { anthropicPool, draftPool, observedGateway, poolCandidates, poolGateway } from '../test/pool-fixtures'
import type { ModelPool } from '../types'
import { PoolEditorDialog } from './PoolEditorDialog'

const api = {
  getEnvironmentCatalog: vi.fn(),
  listGateways: vi.fn(),
  getPoolCandidates: vi.fn(),
  createModelPool: vi.fn(),
  updateModelPool: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

function renderEditor(pool?: ModelPool) {
  const onSaved = vi.fn()
  const onClose = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <FluentProvider theme={webLightTheme}>
        <MemoryRouter>
          <PoolEditorDialog pool={pool} onClose={onClose} onSaved={onSaved} />
        </MemoryRouter>
      </FluentProvider>
    </QueryClientProvider>,
  )
  return { onSaved, onClose }
}

async function openOnGateway(name: string, gatewayId: string) {
  const dialog = await screen.findByRole('dialog', { name })
  await waitFor(() => expect(within(dialog).getByRole('combobox', { name: /^Gateway/ })).toHaveValue(gatewayId))
  return dialog
}

describe('PoolEditorDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [],
      requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [],
      updatedAt: null,
    })
    api.listGateways.mockResolvedValue([poolGateway])
    api.getPoolCandidates.mockResolvedValue(poolCandidates)
  })

  it('walks through a new pool and saves exactly what was chosen', async () => {
    const user = userEvent.setup()
    const created = { ...anthropicPool, id: 'modelpool_new', status: 'draft' as const }
    api.createModelPool.mockResolvedValue(created)
    const { onSaved } = renderEditor()
    const dialog = await openOnGateway('Create a model pool', poolGateway.id)

    await user.type(within(dialog).getByRole('textbox', { name: /^Name/ }), 'Anthropic Claude')
    expect(within(dialog).getByRole('textbox', { name: 'API name' })).toHaveValue('mosaic-pool-anthropic-claude')
    expect(within(dialog).getByRole('textbox', { name: 'API path' })).toHaveValue('mosaic/pool-anthropic-claude')
    expect(
      within(dialog).getByText('Callers send requests to https://apim-contoso-ai.example.test/mosaic/pool-anthropic-claude.'),
    ).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Next' }))

    expect(within(dialog).getByRole('tab', { name: '2. Routing' })).toHaveFocus()
    await user.click(within(dialog).getByRole('radio', { name: /^Preferential/ }))
    const retries = within(dialog).getByRole('spinbutton', { name: /^Retries/ })
    await user.clear(retries)
    await user.type(retries, '2')
    await user.click(within(dialog).getByRole('button', { name: 'Next' }))

    const addModel = await within(dialog).findByRole('combobox', { name: 'Add a model' })
    await user.selectOptions(addModel, 'anthropic|claude-opus-4-5|anthropicmessages')
    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))
    const card = within(dialog).getByRole('region', { name: 'claude-opus-4-5' })
    expect(within(card).getByRole('checkbox', { name: 'Use claude-opus-4-5 on foundry-swedencentral' })).toBeDisabled()
    expect(within(card).getByText('Pools can\'t use endpoints MOSAIC reaches with an API key yet.')).toBeVisible()
    await user.click(within(card).getByRole('checkbox', { name: 'Use claude-opus-4-5 on foundry-westus3' }))
    await user.click(within(dialog).getByRole('button', { name: 'Next' }))

    await user.type(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }), '100000')
    const summary = within(dialog).getByLabelText('Pool summary')
    expect(within(summary).getByText('Anthropic · Anthropic Messages')).toBeVisible()
    expect(within(summary).getByText('1 model, served by 2 deployments')).toBeVisible()
    expect(within(summary).getByText('Preferential, throttling, up to 2 retries')).toBeVisible()
    expect(within(summary).getByText('https://apim-contoso-ai.example.test/mosaic/pool-anthropic-claude')).toBeVisible()
    expect(within(summary).getByText('mosaic-pool-anthropic-claude')).toBeVisible()
    expect(within(dialog).getByText('How callers connect')).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Save and review plan' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(created, true))
    expect(api.createModelPool).toHaveBeenCalledWith({
      displayName: 'Anthropic Claude',
      description: null,
      visibility: 'listed',
      showCapacity: true,
      poolType: 'preferential',
      breakerPreset: 'throttling',
      maxRetries: 2,
      safeguard: { tokensPerMinute: 100000, tokenQuota: null, tokenQuotaPeriod: null },
      models: [
        {
          publicName: 'claude-opus-4-5',
          displayName: 'claude-opus-4-5',
          listed: true,
          allowMixedVersions: false,
          members: [
            { modelEndpointId: 'endpoint_eastus2', deploymentName: 'claude-opus-4-5', weight: 1, drained: false },
            { modelEndpointId: 'endpoint_northcentralus', deploymentName: 'claude-opus-4-5', weight: 1, drained: false },
          ],
        },
      ],
      gatewayId: poolGateway.id,
    })
  })

  it('lists what to fix, and saves nothing, until the pool has a name', async () => {
    const user = userEvent.setup()
    renderEditor()
    const dialog = await openOnGateway('Create a model pool', poolGateway.id)

    await user.click(within(dialog).getByRole('tab', { name: '4. Limits and review' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save as draft' }))

    const problems = within(dialog).getByText('Fix these before saving').closest('[tabindex="-1"]')
    expect(problems).toHaveFocus()
    expect(within(problems as HTMLElement).getByText('Enter a name for the pool.')).toBeVisible()
    expect(within(dialog).getByText('Add at least one model before you publish the pool.')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Save and review plan' })).toBeDisabled()
    expect(api.createModelPool).not.toHaveBeenCalled()
  })

  it('saves only a draft on a gateway MOSAIC can’t publish to', async () => {
    const user = userEvent.setup()
    api.listGateways.mockResolvedValue([observedGateway])
    api.createModelPool.mockResolvedValue({ ...draftPool, gatewayId: observedGateway.id })
    const { onSaved } = renderEditor()
    const dialog = await openOnGateway('Create a model pool', observedGateway.id)

    expect(within(dialog).getByRole('option', { name: 'Fabrikam test gateway (production), drafts only' })).toBeVisible()
    await user.type(within(dialog).getByRole('textbox', { name: /^Name/ }), 'Fabrikam models')
    await user.click(within(dialog).getByRole('tab', { name: '4. Limits and review' }))

    expect(within(dialog).getByRole('button', { name: 'Save and review plan' })).toBeDisabled()
    expect(within(dialog).getByText(/MOSAIC only observes this gateway, so it can’t publish the pool there\./)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Save as draft' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ id: draftPool.id }), false))
    expect(api.createModelPool).toHaveBeenCalledWith(
      expect.objectContaining({ displayName: 'Fabrikam models', gatewayId: observedGateway.id, models: [] }),
    )
  })

  it('moves a new pool off a routing type its gateway can’t run', async () => {
    const user = userEvent.setup()
    const reason = 'This gateway’s tier can’t run backend pools with circuit breakers.'
    api.getPoolCandidates.mockResolvedValue({
      ...poolCandidates,
      poolTypes: { breaker: reason, preferential: reason, linear: null },
    })
    renderEditor()
    const dialog = await openOnGateway('Create a model pool', poolGateway.id)

    await user.click(within(dialog).getByRole('tab', { name: '2. Routing' }))

    await waitFor(() => expect(within(dialog).getByRole('radio', { name: /^Linear/ })).toBeChecked())
    expect(within(dialog).getByRole('radio', { name: /^Breaker/ })).toBeDisabled()
    expect(within(dialog).getByRole('radio', { name: /^Preferential/ })).toBeDisabled()
    expect(within(dialog).getAllByText(reason)).toHaveLength(2)
    expect(
      within(dialog).getByText('A linear pool tries each active deployment once, in order, so it has no retry count.'),
    ).toBeVisible()
  })

  it('saves changes to a published pool as intent the gateway doesn’t run yet', async () => {
    const user = userEvent.setup()
    api.updateModelPool.mockResolvedValue(anthropicPool)
    const { onSaved } = renderEditor(anthropicPool)
    const dialog = await screen.findByRole('dialog', { name: 'Edit Anthropic Claude' })

    expect(within(dialog).getByText('mosaic-pool-anthropic-claude')).toBeVisible()
    expect(within(dialog).queryByRole('textbox', { name: 'API name' })).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('tab', { name: '4. Limits and review' }))
    expect(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveValue(200000)
    expect(
      within(dialog).getByText(
        'Saving changes only MOSAIC’s record. The gateway keeps serving the published pool until you apply a new plan.',
      ),
    ).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(anthropicPool, false))
    expect(api.createModelPool).not.toHaveBeenCalled()
    expect(api.updateModelPool).toHaveBeenCalledWith(anthropicPool.id, {
      displayName: 'Anthropic Claude',
      description: 'Claude models for every team, served from four regions.',
      visibility: 'listed',
      showCapacity: true,
      poolType: 'breaker',
      breakerPreset: 'throttling',
      maxRetries: 3,
      safeguard: { tokensPerMinute: 200000, tokenQuota: null, tokenQuotaPeriod: null },
      models: [
        {
          publicName: 'claude-opus-4-5',
          displayName: 'Claude Opus 4.5',
          listed: true,
          allowMixedVersions: false,
          members: [
            { modelEndpointId: 'endpoint_eastus2', deploymentName: 'claude-opus-4-5', weight: 2, drained: false },
            { modelEndpointId: 'endpoint_northcentralus', deploymentName: 'claude-opus-4-5', weight: 1, drained: false },
            { modelEndpointId: 'endpoint_westus3', deploymentName: 'claude-opus-4-5', weight: 2, drained: true },
          ],
        },
      ],
    })
  })
})
