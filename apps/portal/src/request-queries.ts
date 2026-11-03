import type { QueryClient } from '@tanstack/react-query'

export const resourceRequestsKey = ['portal', 'requests'] as const
export const costCenterRequestsKey = ['portal', 'cost-center-requests'] as const
export const requestCountsKey = ['portal', 'request-counts'] as const

export async function invalidateRequests(queryClient: QueryClient) {
  await Promise.all([
    resourceRequestsKey,
    costCenterRequestsKey,
    requestCountsKey,
    ['portal', 'catalog'],
    ['portal', 'profile'],
    ['portal', 'cost-centers'],
    ['portal', 'notifications'],
  ].map((queryKey) => queryClient.invalidateQueries({ queryKey })))
}
