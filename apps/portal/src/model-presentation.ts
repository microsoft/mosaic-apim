import { resourceTitle } from './entitlement-format'
import type { CatalogEntry, EntitlementResource, PortalModel, ResolvedEntitlement, ResourceSummary } from './types'

export function resourceKey(resource: EntitlementResource) {
  const scoped = ['poolModel', 'modelDeployment', 'product'].includes(resource.kind)
  return JSON.stringify([resource.kind, resource.id, scoped ? resource.scopeId : null])
}

export function modelTitle(
  resource: EntitlementResource,
  displayName: string | null | undefined,
  summary?: ResourceSummary | null,
  model?: PortalModel | null,
) {
  return model?.name.trim() || resourceTitle(resource, displayName, summary)
}

export function catalogTitle(entry: CatalogEntry) {
  return entry.model?.name.trim() || entry.displayName
}

export function grantTitle(resolved: ResolvedEntitlement) {
  return modelTitle(resolved.entitlement.resource, resolved.resourceDisplayName, resolved.resourceSummary, resolved.model)
}

export function modelGroupKey(resource: EntitlementResource, model?: PortalModel | null) {
  // An unknown publisher/format is not evidence that two same-named models are interchangeable.
  if (['modelApi', 'poolModel', 'modelDeployment'].includes(resource.kind) && model?.name.trim() && model.format?.trim()) {
    return JSON.stringify(['model', model.name.trim(), model.format.trim().toLowerCase()])
  }
  return resourceKey(resource)
}

export interface ResourceGroup<T> {
  key: string
  title: string
  items: T[]
}

export function groupResources<T>(items: T[], describe: (item: T) => { key: string; title: string }): ResourceGroup<T>[] {
  const groups = new Map<string, ResourceGroup<T>>()
  for (const item of items) {
    const { key, title } = describe(item)
    const group = groups.get(key)
    if (group) group.items.push(item)
    else groups.set(key, { key, title, items: [item] })
  }
  return [...groups.values()].sort((a, b) => a.title.localeCompare(b.title) || a.key.localeCompare(b.key))
}

export function uniqueValues<T>(values: T[]) {
  return [...new Set(values)]
}
