import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from './api'
import type { CostCenterCreate, CostCenterGroupCapabilities, CostCenterResourceAccess, DirectoryObject } from './types'

export const COST_CENTER_CODE_PATTERN = /^[A-Za-z0-9._-]{1,64}$/

export interface CostCenterDraft {
  name: string
  code: string
  description: string
  owners: string
  keysAllowed: boolean
  setupMode: 'later' | 'attach' | 'create'
  group: DirectoryObject | null
  acknowledge: boolean
  groupName: string
  resourceAccess: CostCenterResourceAccess[]
  suggestionChoice: 'unreviewed' | 'use' | 'ignore'
}

export function newCostCenterDraft(value?: CostCenterCreate): CostCenterDraft {
  return {
    name: value?.name ?? '', code: value?.code ?? '', description: value?.description ?? '',
    owners: value?.owners?.join(', ') ?? '', keysAllowed: value?.keysAllowed ?? true,
    setupMode: value?.groupSetup?.mode ?? 'later',
    group: value?.groupSetup?.mode === 'attach'
      ? { objectId: value.groupSetup.groupObjectId, kind: 'securityGroup', displayName: null } : null,
    acknowledge: value?.groupSetup?.mode === 'attach' && value.groupSetup.acknowledgeMembershipChanges,
    groupName: value?.groupSetup?.mode === 'create' ? value.groupSetup.displayName ?? '' : '',
    resourceAccess: value?.resourceAccess ?? [], suggestionChoice: 'unreviewed',
  }
}

export function costCenterDraftPayload(value: CostCenterDraft): CostCenterCreate {
  return {
    name: value.name.trim(), code: value.code.trim(), description: value.description.trim() || null,
    owners: value.owners.split(/[,\n]/).map((item) => item.trim()).filter(Boolean),
    keysAllowed: value.keysAllowed,
    resourceAccess: value.resourceAccess,
    ...(value.setupMode === 'attach' && value.group && value.acknowledge
      ? { groupSetup: { mode: 'attach' as const, groupObjectId: value.group.objectId, acknowledgeMembershipChanges: true as const } }
      : value.setupMode === 'create' ? { groupSetup: { mode: 'create' as const, displayName: value.groupName.trim() || null } } : {}),
  }
}

export function costCenterDraftErrors(value: CostCenterDraft, capabilities?: CostCenterGroupCapabilities, options?: { exempt?: boolean; bound?: boolean; suggestedGroup?: string | null }) {
  const errors: string[] = []
  if (!value.name.trim() || value.name.trim().length > 120) errors.push('Enter a name between 1 and 120 characters.')
  if (!COST_CENTER_CODE_PATTERN.test(value.code.trim())) errors.push('Use only A-Z, a-z, 0-9, dot, underscore, or hyphen.')
  const owners = value.owners.split(/[,\n]/).map((item) => item.trim()).filter(Boolean)
  if (owners.length > 20 || owners.some((item) => item.length > 254 || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(item))) errors.push('Enter up to 20 valid contact email addresses.')
  if (value.description.length > 1000) errors.push('Keep the description within 1,000 characters.')
  if (options?.suggestedGroup && value.suggestionChoice === 'unreviewed') errors.push('Choose whether to use or ignore the suggested group.')
  if (options?.suggestedGroup && value.suggestionChoice === 'use'
    && (value.setupMode !== 'attach' || value.group?.objectId.toLowerCase() !== options.suggestedGroup.toLowerCase())) errors.push('Explicitly select the suggested group, or ignore the suggestion.')
  if (!options?.exempt && !options?.bound) {
    if (value.setupMode === 'attach' && (!value.group || value.group.kind !== 'securityGroup' || !value.acknowledge)) errors.push('Select a directory security group and acknowledge real membership changes, or finish setup later.')
    if (value.setupMode === 'attach' && (!capabilities?.available || !capabilities.canReadGroups)) errors.push('Group verification is unavailable. Save without setup, or resolve directory read access.')
    if (value.setupMode === 'create' && (!capabilities?.available || !capabilities.canCreateGroups)) errors.push('Group creation needs separately consented Graph permissions. You can finish setup later.')
    if (value.setupMode === 'create' && value.groupName.trim().length > 256) errors.push('Keep the group name within 256 characters.')
  }
  return errors
}

export function useCostCenterCapabilities() {
  const api = useMosaicApi()
  return useQuery({
    queryKey: ['cost-centers', 'group-capabilities'],
    queryFn: () => api.getCostCenterGroupCapabilities(),
    retry: false,
    staleTime: 60_000,
  })
}
