import type { QueryClient } from '@tanstack/react-query'
import type { CostCenter, CostCenterLimit, CostCenterSetupState, PrincipalKind } from './types'

export const COST_CENTER_SETUP_LABELS: Record<CostCenterSetupState, string> = {
  setupRequired: 'Setup required',
  ready: 'Group ready',
  exempt: 'General · Exempt',
}

export function costCenterSetupState(center: Pick<CostCenter, 'setupState'>): CostCenterSetupState {
  // The API derives the exemption from General's stable ID, not its name or builtIn flag.
  return center.setupState ?? 'setupRequired'
}

export function exactCostCenterResourceKey(resource: CostCenterLimit['resource']) {
  return resource.kind === 'poolModel'
    ? `poolModel:${resource.scopeId ?? ''}:${resource.id}`
    : `${resource.kind}:${resource.id}`
}

export function supportedDirectoryMember(kind: PrincipalKind | null): boolean {
  return kind !== null && kind !== 'securityGroup'
}

export function workloadDirectoryMember(kind: PrincipalKind): boolean {
  return kind === 'servicePrincipal' || kind === 'managedIdentity' || kind === 'agentIdentity'
}

export function describeCostCenterAllowance(limit: CostCenterLimit | undefined) {
  if (!limit) return 'No cost-center limit override. Resource safeguards and explicit grant limits still apply.'
  const items: string[] = []
  const n = (value: number) => value.toLocaleString()
  const periods = { Hourly: 'hour', Daily: 'day', Weekly: 'week', Monthly: 'month', Yearly: 'year' }
  const period = (value: keyof typeof periods | null) => periods[value ?? 'Monthly']
  if (limit.person?.tokensPerMinute) items.push(`${n(limit.person.tokensPerMinute)} tokens/minute per grant`)
  if (limit.person?.tokenQuota) items.push(`${n(limit.person.tokenQuota)} tokens/${period(limit.person.tokenQuotaPeriod)} per grant`)
  if (limit.person?.callsPerMinute) items.push(`${n(limit.person.callsPerMinute)} calls/minute per grant`)
  if (limit.person?.callQuota) items.push(`${n(limit.person.callQuota)} calls/${period(limit.person.callQuotaPeriod)} per grant`)
  if (limit.pool?.monthlyTokens) items.push(`${n(limit.pool.monthlyTokens)} pooled tokens/month`)
  if (limit.pool?.monthlyCalls) items.push(`${n(limit.pool.monthlyCalls)} pooled calls/month`)
  return items.join(' · ')
}

export const MEMBERSHIP_PROPAGATION_WARNING = 'Directory propagation and new tokens are required for changed claims. Existing JWTs retain their claims until expiry. Human tokens typically last 60–90 minutes from issuance, not from removal; managed identities may cache tokens or authorization for around 24 hours.'
export const MEMBERSHIP_KEY_WARNING = 'Already copied individual subscription keys are independent credentials. Recheck eligibility, then revoke affected grants or keys and review/apply the gateway changes separately. Removing membership alone does not revoke a key.'

export async function invalidateCostCenterMembership(queryClient: QueryClient, groupId?: string | null) {
  const queryKeys = [['cost-centers'], ['principals'], ['entitlements']]
  if (groupId) queryKeys.push(['groups'], ['memberships', groupId])
  await Promise.all(queryKeys.map((queryKey) => queryClient.invalidateQueries({ queryKey })))
}
