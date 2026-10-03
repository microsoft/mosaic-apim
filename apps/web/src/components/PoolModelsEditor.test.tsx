import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { DraftPoolModel } from '../pools'
import { opusBedrockUsEast1, opusSwedenKeyed, poolCandidates } from '../test/pool-fixtures'
import type { ModelPoolType, PoolCandidateModel } from '../types'
import { PoolModelsEditor } from './PoolModelsEditor'

const OPUS = 'anthropic|claude-opus-4-5|anthropicmessages'

function Harness({
  poolType,
  candidates,
  onDrafts,
}: {
  poolType: ModelPoolType
  candidates: PoolCandidateModel[]
  onDrafts: (drafts: DraftPoolModel[]) => void
}) {
  const [drafts, setDrafts] = useState<DraftPoolModel[]>([])
  return (
    <PoolModelsEditor
      candidates={candidates}
      poolType={poolType}
      drafts={drafts}
      onChange={(next) => {
        setDrafts(next)
        onDrafts(next)
      }}
    />
  )
}

async function addOpus(poolType: ModelPoolType, candidates: PoolCandidateModel[] = poolCandidates.models) {
  const user = userEvent.setup()
  const onDrafts = vi.fn<(drafts: DraftPoolModel[]) => void>()
  render(
    <FluentProvider theme={webLightTheme}>
      <Harness poolType={poolType} candidates={candidates} onDrafts={onDrafts} />
    </FluentProvider>,
  )
  await user.selectOptions(screen.getByRole('combobox', { name: 'Add a model' }), OPUS)
  await user.click(screen.getByRole('button', { name: 'Add model' }))
  const latest = () => onDrafts.mock.lastCall?.[0] ?? []
  return { user, card: screen.getByRole('region', { name: 'claude-opus-4-5' }), latest }
}

const order = (drafts: DraftPoolModel[]) => drafts[0].members.map((member) => member.modelEndpointId)

/** The row for one deployment in a model card's table. */
function rowFor(card: HTMLElement, endpointName: string) {
  return within(card).getByRole('checkbox', { name: `Use claude-opus-4-5 on ${endpointName}` }).closest('tr') as HTMLElement
}

describe('PoolModelsEditor', () => {
  it('adds a model with every deployment that can join, and keeps other vendors out', async () => {
    const { card, latest } = await addOpus('breaker')

    expect(
      screen.getByText(
        'This pool serves Anthropic · Anthropic Messages models. Add more of them, and choose the deployments behind each.',
      ),
    ).toBeVisible()
    expect(within(card).getByRole('checkbox', { name: 'Use claude-opus-4-5 on foundry-eastus2' })).toBeChecked()
    expect(within(card).getByRole('checkbox', { name: 'Use claude-opus-4-5 on foundry-westus3' })).toBeChecked()
    expect(within(card).getByRole('checkbox', { name: 'Use claude-opus-4-5 on foundry-swedencentral' })).toBeChecked()
    const creating = rowFor(card, 'foundry-eastus')
    expect(within(creating).getByRole('checkbox')).not.toBeChecked()
    expect(within(creating).getByRole('checkbox')).toBeDisabled()
    expect(within(creating).getByText('The deployment is Creating.')).toBeVisible()
    expect(within(card).getByRole('textbox', { name: /^Name callers send/ })).toHaveValue('claude-opus-4-5')
    expect(screen.getByRole('option', { name: 'claude-opus-4-5 (already in this pool)' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'gpt-4o (a different vendor or API)' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'claude-sonnet-4-5 (2 of 2 deployments can join)' })).toBeEnabled()
    // Provisioned and pay-as-you-go capacity aren't comparable, so a breaker pool starts them level.
    expect(latest()[0].members.map((member) => member.weight)).toEqual([1, 1, 1, 1])
  })

  it('tries a deployment reached with an API key once, after the backend pool, instead of weighting it', async () => {
    const { card } = await addOpus('breaker')

    const keyed = rowFor(card, 'foundry-swedencentral')
    expect(within(keyed).getByText('API key')).toBeVisible()
    expect(within(keyed).getByText('Declared')).toBeVisible()
    expect(within(keyed).getByText('Tried once, after the backend pool')).toBeVisible()
    expect(within(keyed).queryByRole('spinbutton')).not.toBeInTheDocument()
    expect(within(card).getAllByRole('spinbutton', { name: /^Weight for/ })).toHaveLength(3)
    expect(within(rowFor(card, 'foundry-eastus2')).queryByText('API key')).not.toBeInTheDocument()
    expect(
      within(card).getByText(
        /A deployment reached with an API key can’t join the backend pool, so it’s tried once, after the others\.$/,
      ),
    ).toBeVisible()
  })

  it('marks a deployment on AWS Bedrock, and keeps the name callers send to the model', async () => {
    const [opus, ...others] = poolCandidates.models
    const { card, latest } = await addOpus('breaker', [
      { ...opus, deployments: [...opus.deployments, opusBedrockUsEast1] },
      ...others,
    ])

    const bedrock = within(card)
      .getByRole('checkbox', { name: 'Use us.anthropic.claude-opus-4-5-20251101-v1:0 on bedrock-us-east-1' })
      .closest('tr') as HTMLElement
    expect(within(bedrock).getByText('AWS Bedrock')).toBeVisible()
    // On AWS Bedrock the key and the declaration go without saying.
    expect(within(bedrock).queryByText('API key')).not.toBeInTheDocument()
    expect(within(bedrock).queryByText('Declared')).not.toBeInTheDocument()
    expect(within(bedrock).getByText('Tried second after the backend pool')).toBeVisible()
    expect(within(rowFor(card, 'foundry-swedencentral')).queryByText('AWS Bedrock')).not.toBeInTheDocument()
    expect(within(card).getByRole('textbox', { name: /^Name callers send/ })).toHaveValue('claude-opus-4-5')
    expect(order(latest())).toContain(opusBedrockUsEast1.modelEndpointId)
  })

  it('orders the deployments reached with an API key, and tries them alone once nothing else is active', async () => {
    const norway = {
      ...opusSwedenKeyed,
      modelEndpointId: 'endpoint_norwayeast',
      endpointName: 'foundry-norwayeast',
      region: 'norwayeast',
    }
    const [opus, ...others] = poolCandidates.models
    const { user, card, latest } = await addOpus('preferential', [
      { ...opus, deployments: [...opus.deployments, norway] },
      ...others,
    ])

    expect(within(rowFor(card, 'foundry-swedencentral')).getByText('Tried first after the backend pool')).toBeVisible()
    expect(within(rowFor(card, 'foundry-norwayeast')).getByText('Tried second after the backend pool')).toBeVisible()
    expect(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-swedencentral up' })).toBeDisabled()
    expect(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-norwayeast down' })).toBeDisabled()
    expect(within(card).getByText(/so each is tried once, in the order listed, after the others\.$/)).toBeVisible()

    await user.click(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-norwayeast up' }))
    // The two swap places, and the other deployments keep theirs.
    expect(order(latest())).toEqual([
      'endpoint_eastus2',
      'endpoint_northcentralus',
      'endpoint_westus3',
      'endpoint_norwayeast',
      'endpoint_swedencentral',
    ])
    expect(within(rowFor(card, 'foundry-norwayeast')).getByText('Tried first after the backend pool')).toBeVisible()

    await user.click(within(card).getByRole('switch', { name: 'Drain claude-opus-4-5 on foundry-swedencentral' }))
    expect(within(rowFor(card, 'foundry-swedencentral')).getByText('No requests')).toBeVisible()
    expect(within(rowFor(card, 'foundry-norwayeast')).getByText('Tried once, after the backend pool')).toBeVisible()

    for (const endpoint of ['foundry-eastus2', 'foundry-northcentralus', 'foundry-westus3']) {
      await user.click(within(card).getByRole('checkbox', { name: `Use claude-opus-4-5 on ${endpoint}` }))
    }
    expect(within(rowFor(card, 'foundry-norwayeast')).getByText('Tried once per request')).toBeVisible()
    expect(
      within(card).getByText(/The only active deployment is reached with an API key, so each request tries it once\.$/),
    ).toBeVisible()
    expect(within(card).queryByRole('button', { name: 'Suggest weights from capacity' })).not.toBeInTheDocument()
  })

  it('orders a linear pool’s deployments instead of weighting them', async () => {
    const { user, card, latest } = await addOpus('linear')

    expect(within(card).queryByRole('spinbutton', { name: /^Weight for/ })).not.toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-eastus2 up' })).toBeDisabled()
    expect(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-swedencentral down' })).toBeDisabled()
    // A linear pool tries every deployment as its own target, so one reached with a key is ordered like the rest.
    expect(within(card).queryByText(/Tried once/)).not.toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-eastus2 down' }))

    expect(order(latest())).toEqual([
      'endpoint_northcentralus',
      'endpoint_eastus2',
      'endpoint_westus3',
      'endpoint_swedencentral',
    ])
    expect(within(card).getAllByRole('checkbox', { name: /^Use / })[0]).toHaveAccessibleName(
      'Use claude-opus-4-5 on foundry-northcentralus',
    )
  })

  it('suggests weights from comparable capacity', async () => {
    const { user, card } = await addOpus('breaker')

    await user.click(within(card).getByRole('checkbox', { name: 'Use claude-opus-4-5 on foundry-eastus2' }))
    await user.click(within(card).getByRole('button', { name: 'Suggest weights from capacity' }))

    expect(within(card).getByRole('spinbutton', { name: 'Weight for claude-opus-4-5 on foundry-northcentralus' })).toHaveValue(1)
    expect(within(card).getByRole('spinbutton', { name: 'Weight for claude-opus-4-5 on foundry-westus3' })).toHaveValue(2)
  })

  it('drains a deployment, and removes a model', async () => {
    const { user, card, latest } = await addOpus('preferential')

    // The deployment reached with an API key isn't weighted, so it keeps weight 1.
    expect(latest()[0].members.map((member) => member.weight)).toEqual([1, 1, 2, 1])
    await user.click(within(card).getByRole('switch', { name: 'Drain claude-opus-4-5 on foundry-westus3' }))
    expect(latest()[0].members.map((member) => member.drained)).toEqual([false, false, true, false])

    await user.click(within(card).getByRole('button', { name: 'Remove claude-opus-4-5' }))
    expect(screen.queryByRole('region', { name: 'claude-opus-4-5' })).not.toBeInTheDocument()
    expect(latest()).toEqual([])
    expect(screen.getByRole('option', { name: 'gpt-4o (1 of 1 deployment can join)' })).toBeEnabled()
  })
})
