import { Badge } from '@fluentui/react-components'
import { Link } from 'react-router-dom'
import type { PortalCostCenterRequest } from '../types'

function membershipMessage(request: PortalCostCenterRequest) {
  if (!request.requesterMembershipRequested) return 'The administrator did not request your addition to the group. Submitting or approving this request does not automatically add you.'
  switch (request.requesterMembershipState) {
    case 'succeeded': return 'Your addition to the group was recorded as successful. This is a past operation result, not confirmation of current membership.'
    case 'failed': return 'Your addition to the group failed. Ask the administrator to review membership.'
    case 'unknown': return 'The membership operation outcome is unknown. An administrator must verify it before retrying.'
    default: return 'Your group membership is pending. An administrator must finish or review group setup and the membership step.'
  }
}

export function CostCenterRequestDetails({ request }: { request: PortalCostCenterRequest }) {
  return <>
    <dl className="metadata-list">
      {request.code && <div><dt>Proposed code</dt><dd>{request.code}</dd></div>}
      {request.description && <div><dt>Description</dt><dd>{request.description}</dd></div>}
      <div><dt>Purpose / justification</dt><dd>{request.justification}</dd></div>
      {request.suggestedGroupObjectId && <div><dt>Suggested group object ID</dt><dd>{request.suggestedGroupObjectId}</dd></div>}
      {request.decisionNote && <div><dt>Decision note</dt><dd>{request.decisionNote}</dd></div>}
      {request.costCenter && <div><dt>Created cost center</dt><dd>{request.costCenter.name} ({request.costCenter.code})</dd></div>}
      {request.decidedAt && <div><dt>Decision date</dt><dd>{new Date(request.decidedAt).toLocaleDateString()}</dd></div>}
    </dl>
    {request.suggestedGroupObjectId && <p className="connection-note">The suggested group is not a binding or permission. The administrator chooses and verifies the group.</p>}
    {request.state === 'pending' && request.preparationStarted && <p className="connection-note">An administrator has started preparing this cost center. The request is still pending and can no longer be withdrawn.</p>}
    {request.state === 'approved' && <section className="request-readiness" aria-label="Cost-center readiness">
      <h3>Approval and access are separate</h3>
      <p>Approval links this request to one cost center. It does not confirm that its group, your membership, resource access, or credentials are ready.</p>
      <Badge appearance="outline" color={request.setupState === 'setupRequired' ? 'warning' : 'subtle'}>
        {request.setupState === 'setupRequired' ? 'Group setup required' :
          request.setupState === 'ready' ? 'Group setup complete' :
            request.setupState === 'exempt' ? 'Group setup exempt' : 'Group setup not confirmed'}
      </Badge>
      <p>{membershipMessage(request)}</p>
      {request.requesterMembershipRequested && <p>Directory changes need propagation and new tokens. Existing JWT claims last until expiry, typically 60–90 minutes from issuance for people; managed-identity caches can last around 24 hours. Previously copied keys need separate revocation and gateway apply.</p>}
      <p><Link to="/catalog">Check available resources in the catalog</Link> or <Link to="/access">check gateway status in My access</Link>.</p>
    </section>}
    {request.needsAdministratorAttention && <p role="status" className="connection-note">Administrator attention is needed. Ask your administrator to review setup or the pending membership step.</p>}
  </>
}
