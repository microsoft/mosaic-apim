import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { describe, expect, it } from 'vitest'
import { ChangeReview, TechnicalExecution } from './ChangeReview'
import { rememberEdit, useEditContext } from '../change-review'
import { accessPlan } from '../test/model-access'
import type { PlanReview, PublishPlan } from '../types'

const review: PlanReview = {
  version: 1, baseline: 'applied', appliedAt: '2026-09-01T12:00:00Z',
  changes: [
    { key: 'grants/alex/tokensPerMinute', category: 'Limits', label: 'Alex - tokens/minute', before: '1000', after: '2000', previousKnown: true },
    { key: 'grants/app/enabled', category: 'Access', label: 'Research app - access', before: 'Enabled', after: 'Disabled', previousKnown: true },
  ],
  target: { version: 1, fields: [{ key: 'unchanged', category: 'Routing', label: 'Unchanged route', value: 'chat' }] },
  impact: ['This target temporarily blocks calls, including unchanged callers.', 'Generated policies replace manual edits.'],
}
const plan: PublishPlan = {
  ...accessPlan, review,
  steps: [
    { kind: 'apiPolicy', name: 'chat', resourceId: '/policy', action: 'update', reason: 'Deny', existed: true, stage: 'prepare' },
    { kind: 'apiPolicy', name: 'chat', resourceId: '/policy', action: 'update', reason: 'Activate', existed: true, stage: 'activate' },
  ],
}

describe('ChangeReview', () => {
  it('shows changed-only groups with before/after and persistent impact above collapsed execution', async () => {
    render(<>
      <ChangeReview plan={plan} context={{
        targetId: plan.publicationId,
        fields: [{ key: review.changes[0].key, before: '1500', after: '2000' }],
      }} />
      <TechnicalExecution plan={plan}><p>Ordered write details</p></TechnicalExecution>
    </>)
    expect(within(screen.getByRole('region', { name: 'Your changes' })).getByText('Alex - tokens/minute')).toBeVisible()
    expect(within(screen.getByRole('region', { name: 'Previously saved changes included' })).getByText('Research app - access')).toBeVisible()
    expect(screen.getAllByText('Before')).toHaveLength(2)
    expect(screen.getAllByText('After apply')).toHaveLength(2)
    expect(screen.getByText('Replaces the previously saved value: 1500')).toBeVisible()
    expect(screen.getByText(review.impact[0])).toBeVisible()
    expect(screen.getByText('Unchanged route')).not.toBeVisible()
    expect(screen.getByText('Ordered write details')).not.toBeVisible()
    const summary = screen.getByText('Technical execution (2 planned operations across 1 resource)')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    await userEvent.click(summary)
    expect(screen.getByText('Ordered write details')).toBeVisible()
  })

  it('falls back to pending changes instead of claiming authorship for another target', () => {
    render(<ChangeReview plan={plan} context={{ targetId: 'different', fields: [] }} />)
    expect(screen.getByRole('region', { name: 'Pending changes' })).toBeVisible()
    expect(screen.queryByRole('region', { name: 'Your changes' })).not.toBeInTheDocument()
  })

  it('labels missing baselines unknown and does not silently drop a new category', () => {
    render(<ChangeReview plan={{
      ...plan, review: { ...review, baseline: 'partial', changes: [{
        ...review.changes[0], category: 'Future category', before: null, previousKnown: false,
      }] },
    }} />)
    expect(screen.getByText('Previous value unavailable')).toBeVisible()
    expect(screen.getByText('Future category')).toBeVisible()
    expect(screen.getByText(/Some previous values are unavailable/)).toBeVisible()
  })

  it('keeps the interruption disclosure when recorded intent is equal', () => {
    render(<ChangeReview plan={{ ...plan, review: { ...review, changes: [] } }} />)
    expect(screen.getByText(/No intended configuration changes/)).toBeVisible()
    expect(screen.getByText(review.impact[0])).toBeVisible()
    expect(screen.queryByRole('region', { name: 'Pending changes' })).not.toBeInTheDocument()
  })

  it('does not lose ephemeral edit attribution in React StrictMode', async () => {
    rememberEdit(plan.publicationId, { [review.changes[0].key]: '1000' }, { [review.changes[0].key]: '2000' })
    function Workflow() {
      const [context] = useEditContext(plan.publicationId)
      return <ChangeReview plan={plan} context={context} />
    }
    render(<StrictMode><Workflow /></StrictMode>)
    expect(await screen.findByRole('region', { name: 'Your changes' })).toBeVisible()
  })

  it('keeps older plans fully inspectable without pretending a diff exists', () => {
    render(<><ChangeReview plan={accessPlan} /><TechnicalExecution plan={accessPlan}>Legacy plan</TechnicalExecution></>)
    expect(screen.getByText(/This older plan has no before\/after comparison/)).toBeVisible()
    expect(screen.getByText('Legacy plan')).toBeVisible()
  })

  it('explains unpublish without burying access loss under a full field-by-field removal list', async () => {
    const removal = { ...plan, operation: 'unpublish' as const }
    render(<>
      <ChangeReview plan={removal} />
      <p>Who loses access</p>
      <TechnicalExecution plan={removal}>Deletion steps</TechnicalExecution>
    </>)
    expect(screen.getByText('Who loses access')).toBeVisible()
    expect(screen.getByText(review.impact[0])).toBeVisible()
    expect(screen.getByText('Alex - tokens/minute')).not.toBeVisible()
    await userEvent.click(screen.getByText(/Technical execution \(/))
    await userEvent.click(screen.getByText('Recorded configuration removed (2 values)'))
    expect(screen.getByText('Alex - tokens/minute')).toBeVisible()
  })
})
