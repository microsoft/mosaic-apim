import { assertModelBudgetTarget, cents, ModelProofError, type ModelJourneys } from './model-config.ts'
import type { ModelCaller, ModelObservation } from './model-runtime.ts'

export interface ModelBudgetView {
  id: string
  scope: string
  costCenter: { id: string; name: string; code: string } | null
  amount: number
  recipients: string[]
  notifyOwners: boolean
  action: string
  status: {
    month: string
    monthToDate: number | null
    unpricedTokens: number
    through: string | null
    blocked: boolean
    error: string | null
    crossed: number[]
    notifications: { id: string; kind: string; threshold: number | null; status: string; recipients: number }[]
    gateways: { gatewayId: string; enforcing: boolean; syncedAt: string | null; error: string | null }[]
  }
}

export interface BudgetProofIO {
  read(): Promise<ModelBudgetView | null>
  save(amount: number): Promise<{ startedAt: string; savedAt: string }>
  call: ModelCaller
  banner(blocked: boolean): Promise<void>
  pause(): Promise<void>
}
export interface BudgetProofResult {
  blockSave: { startedAt: string; savedAt: string }
  raiseSave: { startedAt: string; savedAt: string }
  first403: ModelObservation
  firstSuccess: ModelObservation
  blocked: ModelBudgetView
  raised: ModelBudgetView
}

export function pricedBudget(view: ModelBudgetView, scope: ModelJourneys): number {
  assertModelBudgetTarget(scope)
  if (!scope.budget || view.scope !== 'costCenter' || view.costCenter?.id !== scope.budget.grant.costCenterId ||
      view.costCenter.code !== scope.budget.grant.code ||
      view.status.month !== new Date().toISOString().slice(0, 7) || view.status.monthToDate === null ||
      view.status.unpricedTokens !== 0 || view.status.error || !view.status.through || !Number.isFinite(Date.parse(view.status.through))) {
    throw new ModelProofError('Missing current, fully priced isolated monthly rollup; budget proof blocked')
  }
  if (view.recipients.length !== 0 || view.notifyOwners || view.status.notifications.some((n) => n.recipients !== 0)) {
    throw new ModelProofError('This harness does not authorize notification recipients or sends')
  }
  return cents(view.status.monthToDate)
}

export function budgetGateProblems(view: ModelBudgetView, scope: ModelJourneys, blocked: boolean): string[] {
  const expected = scope.budget?.managedGateways.map((g) => g.id).sort()
  const actual = view.status.gateways.map((g) => g.gatewayId).sort()
  const problems: string[] = []
  if (view.status.blocked !== blocked || JSON.stringify(expected) !== JSON.stringify(actual)) problems.push('Budget state must cover ALL managed gateways, not just the grant gateway')
  if (view.status.gateways.some((g) => g.error || g.enforcing !== blocked || !g.syncedAt || !Number.isFinite(Date.parse(g.syncedAt)))) problems.push('Every managed gateway must report the intended block state and sync timestamp')
  if (view.action !== 'block') problems.push('Budget action must remain block')
  return problems
}

/** R12 starts below measured existing spend; R14 crosses from below using actual small priced calls. */
export async function budgetProof(scope: ModelJourneys, journey: 'R12' | 'R14', io: BudgetProofIO): Promise<BudgetProofResult> {
  const fixture = scope.budget
  if (!fixture) throw new ModelProofError('Missing isolated budget fixture')
  assertModelBudgetTarget(scope)
  const before = await io.read()
  if (!before) throw new ModelProofError('Owner must prepare an isolated no-recipient budget through the UI; no implicit setup')
  const spent = pricedBudget(before, scope)
  if (before.status.blocked) throw new ModelProofError('Fixture is already blocked; recover explicitly through UI before running')
  if (journey === 'R12' ? spent < cents(fixture.amount) : spent >= cents(fixture.amount)) {
    throw new ModelProofError(journey === 'R12' ? 'R12 needs real priced spend at or above the blocking amount' : 'R14 must start below the budget in rounded cents')
  }
  await io.call(fixture.grant, fixture.grant.code, 'budget-baseline', 200)
  const blockSave = await io.save(fixture.amount)
  let blocked: ModelBudgetView | undefined
  let first403: ModelObservation | undefined
  let knownSpend = before.status.monthToDate!
  for (let i = 0; i < scope.bounds.maxRequests; i += 1) {
    const mode = journey === 'R14' && cents(knownSpend) < cents(fixture.amount) ? 'budget-spend' : 'budget'
    const probe = await io.call(fixture.grant, fixture.grant.code, `budget-block-probe-${i + 1}`, mode)
    if (probe.status === 200) {
      if (probe.pricedUsageUsd === undefined || !Number.isFinite(probe.pricedUsageUsd) || probe.pricedUsageUsd < 0) throw new ModelProofError('Unknown priced probe usage')
      knownSpend += probe.pricedUsageUsd
    }
    if (probe.status === 403) first403 ??= probe
    const view = await io.read()
    if (!view) throw new ModelProofError('Budget disappeared during proof')
    const currentSpend = pricedBudget(view, scope)
    if (cents(view.amount) !== cents(fixture.amount)) throw new ModelProofError('Saved blocking budget differs from approved amount')
    knownSpend = Math.max(knownSpend, view.status.monthToDate!)
    if (currentSpend >= cents(fixture.raisedAmount)) throw new ModelProofError('Planned raise no longer exceeds measured spend; stop for owner review')
    if (first403 && probe.status === 403 && currentSpend >= cents(fixture.amount) && view.status.blocked &&
        budgetGateProblems(view, scope, true).length === 0) {
      blocked = view
      break
    }
    await io.pause()
  }
  if (!blocked || !first403) throw new ModelProofError('Timed out before a real priced rollup reached and blocked the budget')
  if (journey === 'R14' && Date.parse(blocked.status.through!) <= Date.parse(before.status.through!)) throw new ModelProofError('R14 needs an advancing real priced rollup, not a budget change against old spend')
  if (budgetGateProblems(blocked, scope, true).length) throw new ModelProofError(budgetGateProblems(blocked, scope, true).join('; '))
  const control = scope.selection.other
  await io.call(control, control.code, 'budget-other-cost-center', 200)
  await io.banner(true)
  const raiseSave = await io.save(fixture.raisedAmount)
  let raised: ModelBudgetView | undefined
  let firstSuccess: ModelObservation | undefined
  for (let i = 0; i < scope.bounds.maxRequests; i += 1) {
    const probe = await io.call(fixture.grant, fixture.grant.code, `budget-raise-probe-${i + 1}`, 'budget')
    if (probe.status === 200) firstSuccess ??= probe
    const view = await io.read()
    if (!view) throw new ModelProofError('Budget disappeared after raise')
    if (cents(view.amount) !== cents(fixture.raisedAmount)) throw new ModelProofError('Saved raise differs from approved amount')
    if (pricedBudget(view, scope) >= cents(fixture.raisedAmount)) throw new ModelProofError('Raised budget is not above priced spend')
    if (firstSuccess && probe.status === 200 && budgetGateProblems(view, scope, false).length === 0) { raised = view; break }
    await io.pause()
  }
  if (!raised || !firstSuccess) throw new ModelProofError('Budget raise did not propagate to every managed gateway')
  await io.banner(false)
  return { blockSave, raiseSave, first403, firstSuccess, blocked, raised }
}
