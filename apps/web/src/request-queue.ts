import type { AccessRequest, AdminCostCenterRequest, AdminCostCenterMembershipRequest, AdminRequest } from './types'

export function membershipNeedsAttention(request: AdminCostCenterMembershipRequest) {
  return request.needsAdministratorAttention || (request.state === 'approved' && request.membershipState !== 'succeeded')
}

export function adminRequestKey(kind: AdminRequest['requestKind'], id: string) {
  return `${kind}:${id}`
}

export function combineAdminRequests(resources: AccessRequest[], centers: AdminCostCenterRequest[], memberships: AdminCostCenterMembershipRequest[] = []): AdminRequest[] {
  return [
    ...resources.map((request): AdminRequest => ({ requestKind: 'resource', request })),
    ...centers.map((request): AdminRequest => ({ requestKind: 'costCenter', request })),
    ...memberships.map((request): AdminRequest => ({ requestKind: 'costCenterMembership', request })),
  ].sort((a, b) => b.request.createdAt.localeCompare(a.request.createdAt) || adminRequestKey(a.requestKind, a.request.id).localeCompare(adminRequestKey(b.requestKind, b.request.id)))
}

export function adminRequestLink(kind: AdminRequest['requestKind'], id?: string) {
  const params = new URLSearchParams({ tab: 'requests' })
  if (id) params.set('selected', id)
  if (kind !== 'resource') params.set('requestKind', kind)
  return `/entitlements?${params}`
}
