import type { AdminCostCenterRequest, AdminCostCenterMembershipRequest, CostCenter, CostCenterDirectoryMembers, CostCenterDirectoryOperationView, CostCenterGroupCapabilities } from '../types'

export const directoryTenant = '11111111-1111-4111-8111-111111111111'
export const centerGroupObjectId = '22222222-2222-4222-8222-222222222222'
export const memberObjectId = '33333333-3333-4333-8333-333333333333'

export const centerCapabilities: CostCenterGroupCapabilities = {
  available: true, directoryTenantId: directoryTenant,
  canReadGroups: true, canCreateGroups: true, canAddUsers: true, canAddServicePrincipals: true, canRemoveMembers: true,
  applicationPermissions: ['GroupMember.Read.All', 'Group.Create', 'GroupMember.ReadWrite.All', 'Application.ReadWrite.All'],
  advisory: true, memberListingApi: 'beta',
  memberListingNotice: 'Direct members only. Graph beta preview includes service principals; beta is not supported for production applications.',
}

export const readyCostCenter: CostCenter = {
  id: 'costCenter_fictional_research', tenantId: directoryTenant, entityType: 'costCenter',
  name: 'Research', code: 'research', description: 'Fictional research center', owners: ['finance@example.com'],
  setupState: 'ready',
  entraBinding: { directoryTenantId: directoryTenant, groupObjectId: centerGroupObjectId, provenance: 'attached', verificationState: 'verified', verifiedAt: '2026-11-01T10:00:00Z' },
  resourceAccess: [], members: [], keysAllowed: true, limits: [], builtIn: false, pendingRechecks: [],
  directoryOperations: [], createdAt: '2026-11-01T10:00:00Z', updatedAt: '2026-11-01T10:00:00Z',
  isTenantDefault: false, memberDetails: [], grantCount: 0, enabledGrantCount: 0, defaultFor: 0,
}

export const directoryMemberPage: CostCenterDirectoryMembers = {
  directoryTenantId: directoryTenant, groupObjectId: centerGroupObjectId,
  members: [
    { objectId: memberObjectId, kind: 'user', odataType: '#microsoft.graph.user', displayName: 'Fictional analyst', detail: 'analyst@example.com', appId: null, membershipType: 'direct' },
    { objectId: '44444444-4444-4444-8444-444444444444', kind: 'servicePrincipal', odataType: '#microsoft.graph.servicePrincipal', displayName: 'Fictional workload', detail: null, appId: '55555555-5555-4555-8555-555555555555', membershipType: 'direct' },
    { objectId: '66666666-6666-4666-8666-666666666666', kind: 'securityGroup', odataType: '#microsoft.graph.group', displayName: 'Nested team', detail: null, appId: null, membershipType: 'direct' },
  ],
  nextCursor: null, apiVersion: 'beta', previewNotice: centerCapabilities.memberListingNotice,
}

export const memberOperation: CostCenterDirectoryOperationView = {
  id: 'costCenterMemberOperation_fictional', tenantId: directoryTenant, entityType: 'costCenterDirectoryOperation',
  costCenterId: readyCostCenter.id, kind: 'addMember', directoryTenantId: directoryTenant,
  groupObjectId: centerGroupObjectId, memberObjectId, memberKind: 'user', displayName: null, description: null,
  requestedBy: 'fictional-admin', state: 'succeeded', directoryUpdated: true, canRetry: false,
  propagationNotice: 'Existing JWT claims survive to expiry. Managed identities may cache about 24 hours.',
  createdAt: '2026-11-01T11:00:00Z', updatedAt: '2026-11-01T11:00:00Z',
}

export const costCenterCreationRequest: AdminCostCenterRequest = {
  id: 'request_fictional_center', tenantId: directoryTenant, entityType: 'costCenterRequest', requestKind: 'costCenter',
  requesterObjectId: memberObjectId, name: 'Research', code: 'research', description: 'Fictional research center',
  justification: 'Separate research accounting and resource availability.',
  state: 'pending', needsAdministratorAttention: false,
  createdAt: '2026-11-02T09:00:00Z', updatedAt: '2026-11-02T09:00:00Z',
}

export const costCenterMembershipRequest: AdminCostCenterMembershipRequest = {
  id: 'request_fictional_join', requestKind: 'costCenterMembership',
  requesterObjectId: memberObjectId, costCenter: { id: readyCostCenter.id, name: readyCostCenter.name, code: readyCostCenter.code },
  justification: 'Join the research team for shared inference accounting.',
  state: 'pending', membershipState: null, membershipError: null, operationId: null,
  needsAdministratorAttention: false,
  createdAt: '2026-11-03T09:00:00Z', updatedAt: '2026-11-03T09:00:00Z',
}
