import { useQuery, type QueryClient } from '@tanstack/react-query'
import { useMosaicApi } from './api'
import type {
  EnvironmentCatalogView,
  EnvironmentChangedDetails,
  EnvironmentCompatibilityCell,
  EnvironmentDefinition,
  EnvironmentInUseDetails,
  EnvironmentVerdict,
  GrantsAcknowledgmentRequiredDetails,
  PublicationsBlockedDetails,
} from './types'

export const environmentCatalogQueryKey = ['environment-catalog'] as const
export const environmentSuggestionsQueryKey = ['environment-suggestions'] as const
export const environmentFindingsQueryKey = (gatewayId?: string | null) =>
  ['environment-findings', gatewayId ?? 'all'] as const

export function useEnvironmentCatalog() {
  const api = useMosaicApi()
  return useQuery({ queryKey: environmentCatalogQueryKey, queryFn: () => api.getEnvironmentCatalog() })
}

export function findEnvironment(
  catalog: EnvironmentCatalogView | undefined,
  key: string | null | undefined,
): EnvironmentDefinition | undefined {
  if (key == null) return undefined
  return catalog?.environments.find((environment) => environment.key === key)
}

export function environmentLabel(
  catalog: EnvironmentCatalogView | undefined,
  key: string | null | undefined,
): string {
  if (key == null || key === '') return 'Unclassified'
  return findEnvironment(catalog, key)?.displayName ?? key
}

export function lookupCompatibility(
  catalog: EnvironmentCatalogView | undefined,
  gatewayEnv: string | null | undefined,
  endpointEnv: string | null | undefined,
): EnvironmentCompatibilityCell | undefined {
  const gatewayEnvironment = gatewayEnv ?? null
  const endpointEnvironment = endpointEnv ?? null
  return catalog?.compatibility.find(
    (cell) => cell.gatewayEnvironment === gatewayEnvironment && cell.endpointEnvironment === endpointEnvironment,
  )
}

function detailsWithReason<T extends { reason: string }>(error: unknown, reason: T['reason']): T | undefined {
  const details = (error as { body?: { details?: unknown } } | null | undefined)?.body?.details
  if (typeof details !== 'object' || details == null) return undefined
  return (details as { reason?: unknown }).reason === reason ? (details as T) : undefined
}

export const publicationsBlockedDetails = (error: unknown) =>
  detailsWithReason<PublicationsBlockedDetails>(error, 'publicationsBlocked')
export const grantsAcknowledgmentDetails = (error: unknown) =>
  detailsWithReason<GrantsAcknowledgmentRequiredDetails>(error, 'grantsAcknowledgmentRequired')
export const environmentInUseDetails = (error: unknown) =>
  detailsWithReason<EnvironmentInUseDetails>(error, 'environmentInUse')
export const environmentChangedDetails = (error: unknown) =>
  detailsWithReason<EnvironmentChangedDetails>(error, 'environmentChanged')

export function environmentBlockedVerdict(error: unknown): EnvironmentVerdict | undefined {
  if ((error as { status?: unknown } | null | undefined)?.status !== 409) return undefined
  const details = (error as { body?: { details?: unknown } } | null | undefined)?.body?.details
  if (typeof details !== 'object' || details == null) return undefined
  if ((details as { reason?: unknown }).reason !== 'environmentBlocked') return undefined
  const verdict = (details as { verdict?: unknown }).verdict
  return typeof verdict === 'object' && verdict != null ? (verdict as EnvironmentVerdict) : undefined
}

export function invalidEnvironmentField(error: unknown): string | undefined {
  const details = (error as { body?: { details?: Record<string, unknown> } } | null | undefined)?.body?.details
  if (details?.reason !== 'invalidEnvironment') return undefined
  return typeof details.field === 'string' ? details.field : undefined
}

export function invalidateEnvironmentQueries(queryClient: QueryClient) {
  void queryClient.invalidateQueries({ queryKey: environmentCatalogQueryKey })
  void queryClient.invalidateQueries({ queryKey: environmentSuggestionsQueryKey })
  void queryClient.invalidateQueries({ queryKey: ['environment-findings'] })
  void queryClient.invalidateQueries({ queryKey: ['gateways'] })
  void queryClient.invalidateQueries({ queryKey: ['gateway'] })
  void queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
  void queryClient.invalidateQueries({ queryKey: ['mcp-endpoints'] })
  void queryClient.invalidateQueries({ queryKey: ['model-apis'] })
  void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
}
