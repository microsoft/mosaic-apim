import type { PortalCostCenterMembershipRequest } from './types'

export function membershipOutcome(request: PortalCostCenterMembershipRequest): string {
  if (request.state !== 'approved') return 'Membership has not been added by this request.'
  switch (request.membershipState) {
    case 'succeeded': return 'Membership confirmed'
    case 'failed': return 'Membership addition failed - administrator action needed'
    case 'unknown': return 'Membership outcome unknown - administrator recovery needed'
    default: return 'Approved - membership pending'
  }
}
