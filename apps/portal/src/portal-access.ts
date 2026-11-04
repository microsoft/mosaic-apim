import { resourceFromCatalog, sameResource } from './entitlement-format'
import type { CatalogEntry, ResolvedEntitlement } from './types'

export function isAutomaticAccess(resolved: ResolvedEntitlement) {
  return resolved.accessOrigin === 'automaticCostCenter' || resolved.entitlement.managedBy === 'costCenterAutomatic'
}

export function accessOriginLabel(resolved: ResolvedEntitlement) {
  if (isAutomaticAccess(resolved)) return 'Automatic through your cost center'
  if (resolved.accessOrigin === 'approvedIndividual' || (!resolved.accessOrigin && resolved.via === 'direct')) return 'Approved for you'
  return 'Through your group'
}

export function catalogGrants(entry: CatalogEntry, entitlements: ResolvedEntitlement[]) {
  return entitlements.filter((resolved) => resolved.effective !== false &&
    sameResource(resolved.entitlement.resource, resourceFromCatalog(entry)))
}
