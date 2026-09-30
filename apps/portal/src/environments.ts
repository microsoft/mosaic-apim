import { useQuery } from '@tanstack/react-query'
import { usePortalApi } from './api'
import type { PortalEnvironment } from './types'

export function usePortalEnvironments() {
  const api = usePortalApi()
  return useQuery({
    queryKey: ['portal-environments'],
    queryFn: api.listEnvironments,
    retry: false,
  })
}

export function findEnvironment(list: PortalEnvironment[] | undefined, key: string | null) {
  if (!key) return undefined
  return list?.find((environment) => environment.key === key)
}

export function environmentLabel(list: PortalEnvironment[] | undefined, key: string | null) {
  if (!key) return 'Unclassified'
  return findEnvironment(list, key)?.displayName ?? key
}
