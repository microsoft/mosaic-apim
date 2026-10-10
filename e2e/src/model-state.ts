import type { Targets } from './config.ts'
import { ModelProofError, assertModelBudgetTarget, assertModelReadiness, modelGrants, parseModelJourneys, type ModelJourney, type ModelJourneys } from './model-config.ts'
import { connectionProblems, type ModelConnection } from './model-runtime.ts'
import type { ApiEntitlement, ApiPublication } from './mosaic-api.ts'

export interface ModelCenter {
  id: string
  name: string
  code: string
  description: string | null
  builtIn: boolean
  isTenantDefault: boolean
}

export interface ModelProfile {
  objectId: string
  tenantId: string
  principalId: string | null
  roles: string[]
  isAdmin: boolean
  defaultCostCenter: { id: string; code: string } | null
}

export interface ModelFixtureState {
  publication: ApiPublication
  grants: ApiEntitlement[]
  centers: ReadonlyMap<string, ModelCenter>
  profiles: ReadonlyMap<string, ModelProfile>
  connections: ReadonlyMap<string, ModelConnection>
}

interface ModelReader {
  get<T>(path: string): Promise<T>
}

/** Readers are supplied by approved live preflight; offline planning never constructs them. */
export async function readModelFixtureState(scope: ModelJourneys, api: ModelReader, holders: ReadonlyMap<string, ModelReader>): Promise<ModelFixtureState> {
  const publication = await api.get<ApiPublication>(`/api/v1/publications/${encodeURIComponent(scope.publicationId)}`)
  const grants = await api.get<ApiEntitlement[]>(`/api/v1/entitlements?resource=${encodeURIComponent(scope.modelApiId)}`)
  const centers = new Map<string, ModelCenter>()
  const profiles = new Map<string, ModelProfile>()
  const connections = new Map<string, ModelConnection>()
  for (const g of modelGrants(scope)) {
    const holder = holders.get(g.persona)
    if (!holder) throw new ModelProofError('Missing approved holder reader')
    if (!centers.has(g.costCenterId)) centers.set(g.costCenterId, await api.get<ModelCenter>(`/api/v1/cost-centers/${encodeURIComponent(g.costCenterId)}`))
    if (!profiles.has(g.persona)) profiles.set(g.persona, await holder.get<ModelProfile>('/api/v1/portal/me'))
    connections.set(g.id, await holder.get<ModelConnection>(`/api/v1/me/entitlements/${encodeURIComponent(g.id)}/connection`))
  }
  return { publication, grants, centers, profiles, connections }
}

export function assertModelFixtureState(targets: Targets, scope: ModelJourneys, state: ModelFixtureState, holderObjectIds: ReadonlyMap<string, string>): void {
  parseModelJourneys(scope, targets.personas)
  const { publication, grants, centers, profiles, connections } = state
  const expected = modelGrants(scope)
  const ids = expected.map((g) => g.id).sort()
  if (publication.id !== scope.publicationId || publication.modelApiId !== scope.modelApiId || publication.gatewayId !== scope.gatewayId ||
      !publication.displayName.startsWith(`${scope.ownerTag}-`) || publication.status !== 'published' || publication.accessState !== 'applied') {
    throw new ModelProofError('Publication is not the exact applied, dedicated owner-tagged model fixture')
  }
  const applied = publication.appliedAccess
  if (!applied || JSON.stringify(grants.map((g) => g.id).sort()) !== JSON.stringify(ids) ||
      JSON.stringify(applied.grants.map((g) => g.entitlementId).sort()) !== JSON.stringify(ids)) {
    throw new ModelProofError('Isolated publication has missing or foreign grants in current or applied access')
  }
  for (const g of expected) {
    const profile = profiles.get(g.persona)
    const identity = holderObjectIds.get(g.persona)
    const declared = targets.personas[g.persona]
    if (!profile || !identity || declared?.expectedRole !== 'User' || profile.tenantId !== targets.tenantId ||
        profile.objectId.toLowerCase() !== identity.toLowerCase() ||
        (declared.objectId && declared.objectId.toLowerCase() !== identity.toLowerCase()) ||
        !profile.principalId || profile.isAdmin !== false || !profile.roles.includes('User') || profile.roles.includes('Admin')) {
      throw new ModelProofError('Holder profile identity or User role differs from the approved runtime holder')
    }
    const grant = grants.find((item) => item.id === g.id)
    const snapshot = applied.grants.find((item) => item.entitlementId === g.id)
    const center = centers.get(g.costCenterId)
    const existingDefault = scope.selection.defaultCenterMode === 'existing-read-only' && g.id === scope.selection.default.id
    const matchesCode = (code: string | undefined) => typeof code === 'string' &&
      (existingDefault ? /^[a-z0-9._-]{1,64}$/.test(code.toLowerCase()) && code.toLowerCase() === g.code : code === g.code)
    if (!grant || grant.notes !== scope.ownerTag || !grant.enabled || grant.costCenterId !== g.costCenterId ||
        grant.subject.kind !== 'user' || grant.subject.id !== profile.principalId ||
        grant.resource.kind !== 'modelApi' || grant.resource.id !== scope.modelApiId ||
        grant.runtime?.status !== 'applied' || grant.runtime.publicationId !== scope.publicationId ||
        !center || center.id !== g.costCenterId || !matchesCode(center.code) ||
        (!existingDefault && (center.description !== scope.ownerTag || center.builtIn !== false || center.isTenantDefault !== false))) {
      throw new ModelProofError('Grant/center ownership, identity, ID/code or applied state differs; existing grants are never fixtures')
    }
    const isDefault = profile.defaultCostCenter?.id === g.costCenterId
    if (!snapshot?.enabled || snapshot.costCenterId !== g.costCenterId || !matchesCode(snapshot.costCenterCode) ||
        snapshot.subject?.kind !== 'user' || snapshot.subject.id !== profile.principalId ||
        snapshot.objectId?.toLowerCase() !== identity.toLowerCase() || snapshot.defaultCostCenter !== isDefault ||
        (isDefault && !matchesCode(profile.defaultCostCenter?.code)) ||
        (g.id === scope.selection.default.id && !isDefault)) {
      throw new ModelProofError('Applied grant subject, ID/code or default flag disagrees with the current holder default')
    }
    const connection = connections.get(g.id)
    const problems = connection ? connectionProblems(connection, g, scope) : ['Missing own connection']
    if (connection && (connection.appliedMethods?.entraEnabled !== applied.settings.entraEnabled ||
        connection.appliedMethods?.keysEnabled !== applied.settings.keysEnabled)) problems.push('Own connection access methods disagree with the applied snapshot')
    if (problems.length) throw new ModelProofError(problems.join('; '))
    if (g.id === scope.selection.default.id && scope.approval?.revealExistingKey === true &&
        (snapshot.keysAllowed !== true || connection?.appliedMethods.keysEnabled !== true || !connection.keyExists || !connection.keysAllowedByCostCenter ||
         connection.subscriptionHeader !== 'Ocp-Apim-Subscription-Key')) {
      throw new ModelProofError('Approved default grant existing key is not ready; never create or enable one automatically')
    }
  }
  if (scope.budget) assertModelBudgetTarget(scope)
  const pools = applied?.pools
  if (pools?.length !== 1 || pools[0].costCenterId !== scope.pool.first.costCenterId ||
      pools[0].costCenterCode !== scope.pool.first.code ||
      pools[0].monthlyTokens !== scope.pool.monthlyTokens || pools[0].monthlyCalls != null) {
    throw new ModelProofError('Applied monthly MODEL token pool differs from the exact isolated fixture')
  }
}

/** A failed/changed live check is terminal, even if a later read would look healthy again. */
export function modelActionGuard(targets: Targets, journey: ModelJourney, env: NodeJS.ProcessEnv, readAndValidate: () => Promise<void>): () => Promise<void> {
  let stopped = false
  return async () => {
    if (stopped) throw new ModelProofError('Model fixture guard stopped; no further calls or shared writes')
    try {
      assertModelReadiness(targets, journey, env)
      await readAndValidate()
      assertModelReadiness(targets, journey, env)
    } catch (error) {
      stopped = true
      throw error
    }
  }
}
