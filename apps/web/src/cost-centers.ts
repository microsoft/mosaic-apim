import type { QueryClient } from '@tanstack/react-query'

export async function invalidateCostCenterMembership(queryClient: QueryClient, groupId?: string | null) {
  const queryKeys = [['cost-centers'], ['principals'], ['entitlements']]
  if (groupId) queryKeys.push(['groups'], ['memberships', groupId])
  await Promise.all(queryKeys.map((queryKey) => queryClient.invalidateQueries({ queryKey })))
}
