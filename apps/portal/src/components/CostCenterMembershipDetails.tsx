import { Badge } from '@fluentui/react-components'
import { Link } from 'react-router-dom'
import { membershipOutcome } from '../cost-center-membership'
import type { PortalCostCenterMembershipRequest } from '../types'

export function CostCenterMembershipDetails({ request }: { request: PortalCostCenterMembershipRequest }) {
  return <>
    <dl className="metadata-list">
      <div><dt>Cost center</dt><dd>{request.costCenter.name} ({request.costCenter.code})</dd></div>
      <div><dt>Justification</dt><dd>{request.justification}</dd></div>
      {request.decisionNote && <div><dt>Decision note</dt><dd>{request.decisionNote}</dd></div>}
      <div><dt>Membership</dt><dd>{membershipOutcome(request)}</dd></div>
    </dl>
    {request.needsAdministratorAttention && <Badge appearance="tint" color="warning">Administrator action needed</Badge>}
    {request.state === 'approved' && <p className="connection-note">
      Approval and directory membership are separate. Automatic offerings require an applied grant
      and a fresh token with your group membership. Request-only offerings still need resource approval;
      joining does not create keys or increase the center&apos;s budget.
    </p>}
    <p><Link to="/cost-centers">View My cost centers</Link></p>
  </>
}
