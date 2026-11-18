import type { CostCenterRef, RequestFormSnapshotView } from './types'

export type PocRole = 'technical' | 'billing'
export type ReviewKind = 'resourceAccess' | 'costCenterMembership'
export type ReviewState = 'pending' | 'approved' | 'denied' | 'withdrawn'
export interface ReviewCenter { center: CostCenterRef; roles: PocRole[] }
export interface ReviewItem {
  id: string
  kind: ReviewKind
  title: string
  requester: string | null
  state: ReviewState
  createdAt: string
}
export interface ReviewDetail extends ReviewItem {
  scope: ReviewCenter
  environment: string | null
  justification: string | null
  recordedForm: RequestFormSnapshotView | null
  hasPersonLimits: boolean | null
  canDecide: boolean
  approvalBlocked: boolean
  decisionAt: string | null
  decisionReason: string | null
  decidedByYou: boolean
  membershipState: 'pending' | 'succeeded' | 'failed' | 'unknown' | null
  revision: string
}
export interface ReviewPage { items: ReviewItem[]; nextCursor: string | null }
export interface ReviewDecision {
  revision: string
  reason: string
  acknowledgeMembershipChanges: boolean
}

export const roleLabel = { technical: 'Technical POC', billing: 'Billing POC' } satisfies Record<PocRole, string>
export const stateLabel = {
  pending: 'Pending', approved: 'Approved', denied: 'Denied', withdrawn: 'Withdrawn',
} satisfies Record<ReviewState, string>
export const scopesKey = ['portal', 'approval-centers'] as const
export const approvalsKey = ['portal', 'approvals'] as const
export const approvalPath = (kind: ReviewKind, id: string) => `/approvals/${kind}/${encodeURIComponent(id)}`
