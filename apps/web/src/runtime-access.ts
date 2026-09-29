import type { EnvironmentCompatibilityCell, GatewayRuntimeAccess, RuntimeRoleFinding } from './types'

export interface RuntimeVerdict {
  intent: 'success' | 'warning' | 'error'
  /** A short, plain-language verdict shown next to the gateway's name. */
  label: string
}

export const CAN_INVOKE: RuntimeVerdict = { intent: 'success', label: 'can invoke' }
export const CANNOT_INVOKE: RuntimeVerdict = { intent: 'error', label: 'cannot invoke' }
export const NOT_CONFIRMED: RuntimeVerdict = { intent: 'warning', label: 'not confirmed' }

export function environmentRuntimeVerdict(cell: EnvironmentCompatibilityCell | undefined): RuntimeVerdict {
  if (!cell) return { intent: 'warning', label: 'environment not evaluated' }
  if (cell.level === 'allowed') return { intent: 'success', label: 'environment allowed' }
  if (cell.level === 'warning') return { intent: 'warning', label: 'environment warning' }
  return { intent: 'error', label: 'environment blocked' }
}

/**
 * Whether a gateway can call an endpoint, keyed on what the check found rather than on
 * `evaluation` alone, so that "MOSAIC could not read or evaluate X" is never shown as a denial.
 */
export function runtimeVerdict(access: GatewayRuntimeAccess): RuntimeVerdict {
  switch (access.reason) {
    case 'granted':
      return access.canInvoke ? CAN_INVOKE : NOT_CONFIRMED
    case 'missingRole':
    case 'narrowerScope':
    case 'noGatewayIdentity':
    case 'networkUnreachable':
      return CANNOT_INVOKE
    case 'denyAssignment':
      // A deny that depends on a group or condition MOSAIC cannot evaluate may not apply.
      return access.evaluation === 'notEvaluated' ? NOT_CONFIRMED : CANNOT_INVOKE
    case 'conditional':
    case 'roleUnreadable':
    case 'assignmentsUnreadable':
    case 'identityNotObserved':
    case 'networkUnverified':
      return NOT_CONFIRMED
    default:
      // Recorded before the check reported a reason.
      if (access.evaluation === 'notEvaluated') return NOT_CONFIRMED
      return access.canInvoke ? CAN_INVOKE : CANNOT_INVOKE
  }
}

/** A readable name for an Azure scope. Callers keep the full ID available, for example as a tooltip. */
export function describeScope(scope: string): string {
  const parts = scope.split('/').filter(Boolean)
  const lower = parts.map((part) => part.toLowerCase())
  if (parts.length === 0) return 'the root scope'
  if (lower[0] === 'providers' && lower[2] === 'managementgroups' && parts[3]) {
    return `management group ${parts[3]}`
  }
  const after = (segment: string) => {
    const index = lower.lastIndexOf(segment)
    return index >= 0 ? parts[index + 1] : undefined
  }
  if (lower.includes('accounts')) {
    const project = after('projects')
    if (project) return `project ${project}`
    const account = after('accounts')
    if (account) return account
  }
  if (lower[0] === 'subscriptions' && parts.length === 4 && lower[2] === 'resourcegroups') {
    return `resource group ${parts[3]}`
  }
  if (lower[0] === 'subscriptions' && parts.length === 2) return `subscription ${parts[1]}`
  return scope
}

function roleLabel(finding: RuntimeRoleFinding, startsSentence: boolean): string {
  if (finding.roleName) return finding.roleName
  const article = startsSentence ? 'The' : 'the'
  if (finding.roleDefinitionId) return `${article} role ${finding.roleDefinitionId}`
  return startsSentence ? 'A role' : 'a role'
}

/** Why one of the gateway's role assignments does or does not let it call the published API. */
export function findingSummary(finding: RuntimeRoleFinding): string {
  const role = roleLabel(finding, true)
  const where = describeScope(finding.scope)
  switch (finding.kind) {
    case 'sufficient':
      return `${role} at ${where} covers every operation MOSAIC publishes.`
    case 'insufficient': {
      const [first, ...rest] = finding.missingDataActions
      const missing = first
        ? `${first}${rest.length > 0 ? ` and ${rest.length} more` : ''}`
        : 'every data action the published API needs'
      return `${role} at ${where} does not grant ${missing}.`
    }
    case 'narrowerScope':
      return (
        `${role} at ${where} is assigned below the resource the published API calls, so it ` +
        'does not apply there.'
      )
    case 'conditional':
      return (
        `${role} at ${where} is assigned under an ABAC condition MOSAIC cannot prove holds ` +
        'for these calls, so it is not counted as access.'
      )
    case 'unreadable':
      return (
        `MOSAIC cannot read what ${roleLabel(finding, false)} at ${where} grants. This is not ` +
        'a denial.'
      )
  }
}
