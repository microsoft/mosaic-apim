import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { DraftPoolModel } from '../pools'
import { poolCandidates } from '../test/pool-fixtures'
import type { ModelPoolType } from '../types'
import { PoolModelsEditor } from './PoolModelsEditor'

const OPUS = 'anthropic|claude-opus-4-5|anthropicmessages'

function Harness({ poolType, onDrafts }: { poolType: ModelPoolType; onDrafts: (drafts: DraftPoolModel[]) => void }) {
  const [drafts, setDrafts] = useState<DraftPoolModel[]>([])
  return (
    <PoolModelsEditor
      candidates={poolCandidates.models}
      poolType={poolType}
      drafts={drafts}
      onChange={(next) => {
        setDrafts(next)
        onDrafts(next)
      }}
    />
  )
}

async function addOpus(poolType: ModelPoolType) {
  const user = userEvent.setup()
  const onDrafts = vi.fn<(drafts: DraftPoolModel[]) => void>()
  render(
    <FluentProvider theme={webLightTheme}>
      <Harness poolType={poolType} onDrafts={onDrafts} />
    </FluentProvider>,
  )
  await user.selectOptions(screen.getByRole('combobox', { name: 'Add a model' }), OPUS)
  await user.click(screen.getByRole('button', { name: 'Add model' }))
  const latest = () => onDrafts.mock.lastCall?.[0] ?? []
  return { user, card: screen.getByRole('region', { name: 'claude-opus-4-5' }), latest }
}

const order = (drafts: DraftPoolModel[]) => drafts[0].members.map((member) => member.modelEndpointId)

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
    expect(within(card).getByRole('checkbox', { name: 'Use claude-opus-4-5 on foundry-swedencentral' })).not.toBeChecked()
    expect(within(card).getByRole('textbox', { name: /^Name callers send/ })).toHaveValue('claude-opus-4-5')
    expect(screen.getByRole('option', { name: 'claude-opus-4-5 (already in this pool)' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'gpt-4o (a different vendor or API)' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'claude-sonnet-4-5 (2 of 2 deployments can join)' })).toBeEnabled()
    // Provisioned and pay-as-you-go capacity aren't comparable, so a breaker pool starts them level.
    expect(latest()[0].members.map((member) => member.weight)).toEqual([1, 1, 1])
  })

  it('orders a linear pool’s deployments instead of weighting them', async () => {
    const { user, card, latest } = await addOpus('linear')

    expect(within(card).queryByRole('spinbutton', { name: /^Weight for/ })).not.toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-eastus2 up' })).toBeDisabled()
    expect(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-westus3 down' })).toBeDisabled()
    await user.click(within(card).getByRole('button', { name: 'Move claude-opus-4-5 on foundry-eastus2 down' }))

    expect(order(latest())).toEqual(['endpoint_northcentralus', 'endpoint_eastus2', 'endpoint_westus3'])
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

    expect(latest()[0].members.map((member) => member.weight)).toEqual([1, 1, 2])
    await user.click(within(card).getByRole('switch', { name: 'Drain claude-opus-4-5 on foundry-westus3' }))
    expect(latest()[0].members.map((member) => member.drained)).toEqual([false, false, true])

    await user.click(within(card).getByRole('button', { name: 'Remove claude-opus-4-5' }))
    expect(screen.queryByRole('region', { name: 'claude-opus-4-5' })).not.toBeInTheDocument()
    expect(latest()).toEqual([])
    expect(screen.getByRole('option', { name: 'gpt-4o (1 of 1 deployment can join)' })).toBeEnabled()
  })
})
