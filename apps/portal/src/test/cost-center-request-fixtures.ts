import type { PortalCostCenterRequest } from '../types'

export function costCenterRequest(overrides: Partial<PortalCostCenterRequest> = {}): PortalCostCenterRequest {
  return {
    id: 'center-request-1',
    requestKind: 'costCenter',
    name: 'Research team',
    justification: 'Charge our evaluation work separately.',
    state: 'pending',
    createdAt: '2026-10-28T10:00:00Z',
    updatedAt: '2026-10-28T10:00:00Z',
    preparationStarted: false,
    requesterMembershipRequested: false,
    needsAdministratorAttention: false,
    ...overrides,
  }
}
