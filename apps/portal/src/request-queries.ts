import type { QueryClient } from '@tanstack/react-query'

export const resourceRequestsKey = ['portal', 'requests'] as const
export const costCenterRequestsKey = ['portal', 'cost-center-requests'] as const
export const membershipRequestsKey = ['portal', 'cost-center-membership-requests'] as const
export const costCenterPolicyKey = ['portal', 'cost-center-self-service-settings'] as const
export const myCostCentersKey = ['portal', 'my-cost-centers'] as const
export const costCenterDirectoryKey = ['portal', 'cost-center-directory'] as const
export const requestCountsKey = ['portal', 'request-counts'] as const

export async function invalidateRequests(queryClient: QueryClient) {
  await Promise.all([
    resourceRequestsKey,
    costCenterRequestsKey,
    membershipRequestsKey,
    myCostCentersKey,
    costCenterDirectoryKey,
    requestCountsKey,
    ['portal', 'catalog'],
    ['portal', 'entitlements'],
    ['portal', 'profile'],
    ['portal', 'cost-centers'],
    ['portal', 'notifications'],
  ].map((queryKey) => queryClient.invalidateQueries({ queryKey })))
}
