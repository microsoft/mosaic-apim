import type { AccessRequest, AdminCostCenterRequest, AdminRequest } from './types'

export function adminRequestKey(kind: AdminRequest['requestKind'], id: string) {
  return `${kind}:${id}`
}

export function combineAdminRequests(resources: AccessRequest[], centers: AdminCostCenterRequest[]): AdminRequest[] {
  return [
    ...resources.map((request): AdminRequest => ({ requestKind: 'resource', request })),
    ...centers.map((request): AdminRequest => ({ requestKind: 'costCenter', request })),
  ].sort((a, b) => b.request.createdAt.localeCompare(a.request.createdAt) || adminRequestKey(a.requestKind, a.request.id).localeCompare(adminRequestKey(b.requestKind, b.request.id)))
}

export function adminRequestLink(kind: AdminRequest['requestKind'], id?: string) {
  const params = new URLSearchParams({ tab: 'requests' })
  if (id) params.set('selected', id)
  if (kind === 'costCenter') params.set('requestKind', kind)
  return `/entitlements?${params}`
}
