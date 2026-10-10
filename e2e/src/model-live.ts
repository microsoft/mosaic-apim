import type { TestInfo } from '@playwright/test'
import { type Targets, persona } from './config.ts'
import { type PersonaPool, expect } from './fixtures.ts'
import { skip } from './journeys.ts'
import { type ModelJourney, type ModelJourneys, ModelProofError, assertModelBudgetTarget, assertModelReadiness, modelGrants, modelReadiness, modelScopeHash } from './model-config.ts'
import type { ModelBudgetView } from './model-budget.ts'
import { ModelAllowance, type ModelConnection, type ModelObservation, boundedModelCaller, modelRoute } from './model-runtime.ts'
import { type ModelFixtureState, assertModelFixtureState, modelActionGuard, readModelFixtureState } from './model-state.ts'
import { MosaicApi } from './mosaic-api.ts'
import { MyAccessPage } from './pages/portal/my-access.ts'
import { tokenIsFor } from './runtime.ts'
import { tokenClaims } from './verify.ts'

interface PricingEndpoint {
  endpointId: string
  cloud: string | null
  region: string | null
  regionSource: string | null
  deployments: { deploymentName: string; model: string | null; version: string | null; deploymentType: string | null; deploymentTypeSource: string | null; declared: boolean; priced: boolean; priceId: string | null }[]
}
interface Price {
  id: string
  inputPerMillion: number | null
  outputPerMillion: number | null
  cachedInputPerMillion: number | null
  effectiveFrom: string
  effectiveUntil: string | null
  ptuHourly: number | null
  monthlyAmount: number | null
}

export function approvedModelScope(targets: Targets, journey: ModelJourney): ModelJourneys {
  const problems = modelReadiness(targets, journey, process.env)
  if (problems.length) skip(problems.join('; '))
  return targets.modelJourneys as ModelJourneys
}

export const modelTokenVariable = (personaKey: string) => `MOSAIC_E2E_MODEL_TOKEN_${personaKey.replaceAll('-', '_').toUpperCase()}`

export function runtimeTokenProblems(token: string | undefined, targets: Targets, holder: string, c: ModelConnection): string[] {
  if (!token || !tokenIsFor(token, persona(targets, holder))) return ['Missing or wrong-holder dedicated model runtime token']
  const claims = tokenClaims(token)
  const audience = typeof claims?.aud === 'string' ? claims.aud.replace(/^api:\/\//, '').toLowerCase() : ''
  if (typeof c.entraAudience !== 'string' || !c.entraAudience || c.tenantId !== targets.tenantId ||
      audience !== c.entraAudience.toLowerCase() || claims?.tid !== targets.tenantId ||
      typeof claims.exp !== 'number' || claims.exp < Date.now() / 1000 + targets.modelJourneys!.bounds.timeoutSeconds + 60 ||
      typeof claims.scp !== 'string' || !claims.scp.split(' ').includes('Models.Invoke')) {
    return ['Runtime token lacks the exact tenant, audience, Models.Invoke scope or full-run lifetime']
  }
  return []
}

/** All preflight HTTP is read-only and runs only AFTER exact-scope approval, never during offline validation. */
export async function prepareModelRun(personas: PersonaPool, targets: Targets, journey: ModelJourney, testInfo: TestInfo) {
  const scope = approvedModelScope(targets, journey)
  for (const grant of modelGrants(scope)) {
    if (!process.env[modelTokenVariable(grant.persona)]) skip(`Missing ${modelTokenVariable(grant.persona)}; no automatic runtime sign-in or key creation`)
  }
  const api = await MosaicApi.as(personas, targets, targets.roles.admin)
  const tokens = new Map<string, string>()
  const holders = new Map<string, MosaicApi>()
  for (const g of modelGrants(scope)) {
    tokens.set(g.persona, process.env[modelTokenVariable(g.persona)]!.trim())
    if (!holders.has(g.persona)) holders.set(g.persona, await MosaicApi.as(personas, targets, g.persona))
  }
  const connections = new Map<string, ModelConnection>()
  let prepared: ModelFixtureState | undefined
  const guard = modelActionGuard(targets, journey, process.env, async () => {
    const email = await api.get<{ enabled: boolean }>('/api/v1/settings/email')
    if (email.enabled !== false) throw new ModelProofError('Automatic budget email must remain disabled; never change settings or risk delivery')
    const current = await readModelFixtureState(scope, api, holders)
    const identities = new Map<string, string>()
    for (const g of modelGrants(scope)) {
      const c = current.connections.get(g.id)!
      const token = tokens.get(g.persona)
      const problems = runtimeTokenProblems(token, targets, g.persona, c)
      const objectId = token ? tokenClaims(token)?.oid : undefined
      if (problems.length || typeof objectId !== 'string' || !objectId) throw new ModelProofError('Runtime holder token is not ready or has no exact object identity')
      identities.set(g.persona, objectId)
      modelRoute(c, targets.origins.gateway)
    }
    assertModelFixtureState(targets, scope, current, identities)
    for (const [id, c] of current.connections) connections.set(id, c)
    prepared = current
  })
  await guard()
  const { centers, publication } = prepared!
  for (const g of modelGrants(scope)) {
    const center = centers.get(g.costCenterId)!
    // Same-model cards are disambiguated by cost center before their connection panels open.
    const access = await MyAccessPage.open(personas, targets, g.persona)
    const card = access.card(publication.displayName).filter({ hasText: `${center.name} · ${center.code}` })
    await expect(card).toHaveCount(1)
    const panel = await access.connectionDetails(card)
    await expect(panel.section('Cost center header')).toBeVisible()
    await expect(panel.root.getByText(center.code, { exact: true }).first()).toBeVisible()
  }
  const pricing = await api.get<PricingEndpoint[]>('/api/v1/pricing/endpoints')
  const endpoint = pricing.find((e) => e.endpointId === publication.modelEndpointId)
  const facts = endpoint?.deployments.find((d) => d.deploymentName === publication.deploymentName)
  if (!endpoint?.cloud || !facts?.priced || !facts.priceId || facts.model !== scope.price.actualModel ||
      facts.version !== scope.price.version || facts.deploymentType !== scope.price.deploymentType || endpoint.region !== scope.price.region) {
    skip('Actual ARM model/type/region facts are unpriced or disagree with approved pricing; never price by deployment alias')
  }
  if (facts.declared || facts.deploymentTypeSource !== 'observed' || endpoint.regionSource !== 'detected') skip('Model pricing facts must be observed ARM deployment facts, not declared/admin guesses')
  const prices = await api.get<{ asOf: string; lines: { current: Price | null }[] }>(`/api/v1/pricing/prices?cloud=${encodeURIComponent(endpoint.cloud)}`)
  const price = prices.lines.map((l) => l.current).find((p) => p?.id === facts.priceId)
  if (prices.asOf !== scope.price.date || !price || price.inputPerMillion !== scope.price.inputPerMillion ||
      price.outputPerMillion !== scope.price.outputPerMillion || price.ptuHourly !== null || price.monthlyAmount !== null ||
      price.cachedInputPerMillion !== scope.price.cachedInputPerMillion ||
      (price.cachedInputPerMillion !== null && price.cachedInputPerMillion > scope.price.inputPerMillion) ||
      price.effectiveFrom > scope.price.date || (price.effectiveUntil !== null && price.effectiveUntil < scope.price.date)) {
    skip('Current exact matched price differs or has unsupported capacity pricing; no billed calls authorized')
  }
  const observations: ModelObservation[] = []
  const startedAt = Date.now()
  const allowance = new ModelAllowance(scope)
  const call = boundedModelCaller(scope, targets.origins.gateway, connections, tokens, async (url, headers, body) => {
    assertModelReadiness(targets, journey, process.env)
    try {
      const response = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(30_000) })
      const text = await response.text()
      let parsed: unknown = text
      try { parsed = JSON.parse(text) } catch { /* Gateway denials may be plain text; success must be real JSON. */ }
      return { status: response.status, headers: Object.fromEntries(response.headers.entries()), body: parsed }
    } catch {
      throw new ModelProofError('Model HTTP failed; no URL, credential or response body retained')
    }
  }, allowance, (entry) => observations.push(entry), guard)
  const finish = async (details: object = {}) => {
    // Deliberately no bodies, headers, tokens, endpoints, email addresses or raw API objects.
    await testInfo.attach(`${journey}-model-evidence.json`, {
      body: JSON.stringify({ journey, scopeSha256: modelScopeHash(targets), fullJourney: 'NOT PROVEN',
        boundary: 'Runtime/UI evidence only; independent analytics/backend/trace/ARM audit/mailbox evidence is still required',
        observations, allowance: allowance.summary(), ...details }, null, 2), contentType: 'application/json',
    })
    tokens.clear()
  }
  const pause = async () => {
    if (Date.now() > startedAt + scope.bounds.timeoutSeconds * 1000) throw new ModelProofError('Model journey deadline reached')
    await new Promise((resolve) => setTimeout(resolve, scope.bounds.intervalSeconds * 1000))
  }
  return { scope, api, centers, connections, call, observations, allowance, finish, pause, guard }
}

export async function budgetReady(api: MosaicApi, scope: ModelJourneys): Promise<void> {
  assertModelBudgetTarget(scope)
  const fixture = scope.budget
  if (!fixture) skip('Missing isolated budget scope')
  const gateways = (await api.get<(Awaited<ReturnType<MosaicApi['gateways']>>[number] & { capabilities: { skuName: string | null } })[]>('/api/v1/gateways')).filter((g) => g.managementMode === 'manage')
  if (JSON.stringify(gateways.map((g) => g.id).sort()) !== JSON.stringify(fixture.managedGateways.map((g) => g.id).sort()) ||
      gateways.some((g) => !g.access.canWrite)) throw new ModelProofError('ALL current managed gateways must be available and in approved budget scope')
  for (const gateway of gateways) {
    const sku = gateway.capabilities?.skuName?.toLowerCase()
    const actualTier = sku?.endsWith('v2') ? 'v2' : ['developer', 'basic', 'standard', 'premium'].includes(sku ?? '') ? 'classic' : undefined
    if (!actualTier || fixture.managedGateways.find((g) => g.id === gateway.id)?.tier !== actualTier) skip('Managed gateway tier is unknown or differs from approved scope; no v2 claim inferred')
  }
  const status = await api.get<{ dataSource: string; rollupsEnabled: boolean }>('/api/v1/analytics/status')
  const overview = await api.get<{ priced: boolean }>('/api/v1/budgets')
  if (status.dataSource !== 'logAnalytics' || !status.rollupsEnabled || !overview.priced) skip('Budget proof needs real enabled priced rollups, not simulated/source evidence')
  const current = await api.get<ModelBudgetView | null>(`/api/v1/cost-centers/${encodeURIComponent(fixture.grant.costCenterId)}/budget`)
  if (!current) skip('Owner must set an isolated no-recipient preparation budget in the UI first')
  if (current.notifyOwners || current.recipients.length || current.status.notifications.some((n) => n.recipients > 0)) throw new ModelProofError('No notification sends are authorized by this harness')
}
