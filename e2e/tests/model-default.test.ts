import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { assertModelBudgetTarget, modelGrants, modelReadiness, modelScopeHash, parseModelJourneys, type ModelJourneys } from '../src/model-config.ts'
import { pricedBudget } from '../src/model-budget.ts'
import { ModelAllowance, boundedModelCaller } from '../src/model-runtime.ts'
import { assertModelFixtureState, modelActionGuard, readModelFixtureState } from '../src/model-state.ts'
import { budget, modelFixture as fixture, modelTargets, success } from './model-fixtures.ts'
import { e2eRoot } from '../src/paths.ts'

function approve(targets: ReturnType<typeof modelTargets>) {
  const scope = targets.modelJourneys!
  scope.price.date = new Date().toISOString().slice(0, 10)
  scope.approval = {
    reference: 'offline-only', expiresAt: new Date(Date.now() + 3600_000).toISOString(), scopeSha256: modelScopeHash(targets),
    journeys: ['R10', 'R11', 'R12', 'R14'], revealExistingKey: true, budgetWritesAcrossAllManagedGateways: true,
  }
  return { MOSAIC_E2E_MODEL_SCOPE: scope.ownerTag, MOSAIC_E2E_MODEL_JOURNEY: 'R10' }
}

test('absent default mode preserves the old parsed shape, hash and strict all-owned behavior', () => {
  const f = fixture()
  const parsed = parseModelJourneys(f.scope, f.targets.personas)
  assert.deepEqual(parsed, f.scope)
  assert.equal(Object.hasOwn(parsed.selection, 'defaultCenterMode'), false)
  const oldHash = createHash('sha256').update(JSON.stringify({
    protocolVersion: 1, owned: f.scope, origins: f.targets.origins, tenantId: f.targets.tenantId,
    admin: f.targets.personas[f.targets.roles.admin], holders: modelGrants(f.scope).map((g) => f.targets.personas[g.persona]),
  })).digest('hex')
  assert.equal(modelScopeHash(f.targets), oldHash)
  f.validate()
  f.centers.get(f.scope.selection.default.costCenterId)!.builtIn = true
  assert.throws(f.validate, /ownership/)
  f.scope.selection.default.code = 'general'
  assert.throws(() => parseModelJourneys(f.scope, f.targets.personas), /ownerTag-prefixed/)
  f.scope.selection.defaultCenterMode = 'owned'
  assert.throws(() => parseModelJourneys(f.scope, f.targets.personas), /ownerTag-prefixed/)
})

test('explicit existing-read-only references only the effective default, with five new owned grants and three owned centers', () => {
  const f = fixture(true)
  assert.deepEqual(parseModelJourneys(f.scope, f.targets.personas), f.scope)
  assert.equal(modelGrants(f.scope).length, 5)
  f.validate()
  const before = structuredClone(f.state)
  f.validate()
  assert.deepEqual(f.state, before)
  f.centers.get(f.scope.selection.default.costCenterId)!.code = 'GENERAL'
  f.profiles.get(f.scope.selection.default.persona)!.defaultCostCenter!.code = 'GENERAL'
  f.state.publication.appliedAccess!.grants[0].costCenterCode = 'GENERAL'
  f.connections.get(f.scope.selection.default.id)!.costCenter.code = 'GENERAL'
  f.validate()
  for (const g of [f.scope.selection.other, f.scope.pool.first, f.scope.budget!.grant]) {
    const center = f.centers.get(g.costCenterId)!
    center.description = 'Unrelated'
    assert.throws(f.validate, /ownership/)
    center.description = f.scope.ownerTag
    center.isTenantDefault = true
    assert.throws(f.validate, /ownership/)
    center.isTenantDefault = false
    center.builtIn = true
    assert.throws(f.validate, /ownership/)
    center.builtIn = false
  }
})

test('read-only default codes use a canonical lowercase backend-safe subset, bounded to 64, never arbitrary text', () => {
  const f = fixture(true)
  for (const code of ['general', 'team-42', 'a.b', 'a_b', 'a'.repeat(64)]) {
    f.scope.selection.default.code = code
    assert.equal(parseModelJourneys(f.scope, f.targets.personas).selection.default.code, code)
  }
  for (const code of ['', 'a'.repeat(65), 'GENERAL', ' leading', 'trailing ', 'a,b', 'a/b', '\u00e9']) {
    f.scope.selection.default.code = code
    assert.throws(() => parseModelJourneys(f.scope, f.targets.personas), /invalid value/)
  }
  f.scope.selection.default.code = 'general'
  for (const g of [f.scope.selection.other, f.scope.pool.first, f.scope.pool.second, f.scope.budget!.grant]) {
    const code = g.code
    g.code = 'general'
    assert.throws(() => parseModelJourneys(f.scope, f.targets.personas))
    g.code = code
  }
  assert.throws(() => parseModelJourneys({ ...f.scope, selection: { ...f.scope.selection, defaultCenterMode: 'shared' } }, f.targets.personas), /defaultCenterMode/)
  const long = fixture()
  long.scope.ownerTag = `e2e-model-${'a'.repeat(40)}`
  for (const g of modelGrants(long.scope)) g.code = `${long.scope.ownerTag}-${'b'.repeat(16)}`
  assert.throws(() => parseModelJourneys(long.scope, long.targets.personas), /invalid value/)
})

test('all selection, pool and budget center IDs/codes and every grant remain distinct in read-only mode', () => {
  for (const change of [
    (s: ModelJourneys) => { s.selection.other.costCenterId = s.selection.default.costCenterId },
    (s: ModelJourneys) => { s.pool.first.costCenterId = s.pool.second.costCenterId = s.selection.default.costCenterId },
    (s: ModelJourneys) => { s.budget!.grant.costCenterId = s.selection.default.costCenterId },
    (s: ModelJourneys) => { s.selection.default.code = s.selection.other.code },
    (s: ModelJourneys) => { s.pool.second.id = s.selection.default.id },
    (s: ModelJourneys) => { s.budget!.grant.id = s.selection.default.id },
  ]) {
    const f = fixture(true)
    change(f.scope)
    assert.throws(() => parseModelJourneys(f.scope, f.targets.personas))
  }
})

test('mode, selected default ID/code and holder identity/role are bound to exact approval', () => {
  for (const change of [
    (f: ReturnType<typeof fixture>) => { f.scope.selection.defaultCenterMode = 'owned' },
    (f: ReturnType<typeof fixture>) => { f.scope.selection.default.costCenterId = 'cc_changed' },
    (f: ReturnType<typeof fixture>) => { f.scope.selection.default.code = 'changed' },
    (f: ReturnType<typeof fixture>) => { f.targets.personas[f.scope.selection.default.persona].objectId = 'changed' },
    (f: ReturnType<typeof fixture>) => { f.targets.personas[f.scope.selection.default.persona].expectedRole = 'Admin' },
  ]) {
    const f = fixture(true)
    const env = approve(f.targets)
    assert.deepEqual(modelReadiness(f.targets, 'R10', env), [])
    const hash = modelScopeHash(f.targets)
    change(f)
    assert.notEqual(modelScopeHash(f.targets), hash)
    assert.match(modelReadiness(f.targets, 'R10', env).join(), /changed exact-scope/)
  }
})

test('read-only default still rejects foreign grants/centers, wrong subjects, roles, applied flags and stale own readiness', () => {
  for (const change of [
    (f: ReturnType<typeof fixture>) => { f.state.grants[0].notes = 'unrelated' },
    (f: ReturnType<typeof fixture>) => { f.state.grants[0].subject.id = 'foreign-principal' },
    (f: ReturnType<typeof fixture>) => { f.state.grants[0].subject.kind = 'securityGroup' },
    (f: ReturnType<typeof fixture>) => { f.state.grants[0].resource.id = 'foreign-api' },
    (f: ReturnType<typeof fixture>) => { f.state.grants[0].enabled = false },
    (f: ReturnType<typeof fixture>) => { f.state.grants[0].runtime!.status = 'pending' },
    (f: ReturnType<typeof fixture>) => { f.state.grants[0].runtime!.publicationId = 'foreign-publication' },
    (f: ReturnType<typeof fixture>) => { f.state.grants.push({ ...f.state.grants[0], id: 'foreign-grant' }) },
    (f: ReturnType<typeof fixture>) => { f.state.grants.pop() },
    (f: ReturnType<typeof fixture>) => { f.centers.get(f.scope.selection.default.costCenterId)!.id = 'foreign-center' },
    (f: ReturnType<typeof fixture>) => { f.centers.get(f.scope.selection.default.costCenterId)!.code = 'foreign' },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.defaultCostCenter!.id = 'foreign-center' },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.defaultCostCenter!.code = 'foreign' },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.defaultCostCenter = null },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.objectId = 'foreign-holder' },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.tenantId = 'foreign-tenant' },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.roles = [] },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.isAdmin = true },
    (f: ReturnType<typeof fixture>) => { f.profiles.get(f.scope.selection.default.persona)!.principalId = null },
    (f: ReturnType<typeof fixture>) => { f.identities.delete(f.scope.selection.default.persona) },
    (f: ReturnType<typeof fixture>) => { f.targets.personas[f.scope.selection.default.persona].objectId = 'foreign-declared' },
    (f: ReturnType<typeof fixture>) => { f.state.publication.displayName = 'unrelated' },
    (f: ReturnType<typeof fixture>) => { f.state.publication.id = 'foreign-publication' },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.settings.keysEnabled = false },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants.push({ entitlementId: 'foreign', enabled: true }) },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants.pop() },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[0].costCenterId = 'foreign' },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[0].costCenterCode = 'foreign' },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[0].subject = { kind: 'user', id: 'foreign' } },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[0].objectId = 'foreign' },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[0].defaultCostCenter = false },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[1].defaultCostCenter = true },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[2].defaultCostCenter = true },
    (f: ReturnType<typeof fixture>) => { f.connections.get(f.scope.selection.default.id)!.costCenter.id = 'foreign' },
    (f: ReturnType<typeof fixture>) => { f.connections.get(f.scope.selection.default.id)!.grantLimits.tokens!.tokenQuota = 1 },
    (f: ReturnType<typeof fixture>) => { f.connections.get(f.scope.selection.default.id)!.runtime.status = 'pending' },
    (f: ReturnType<typeof fixture>) => { f.connections.get(f.scope.selection.default.id)!.appliedMethods.entraEnabled = false },
  ]) {
    const f = fixture(true)
    change(f)
    assert.throws(f.validate)
  }
  for (const change of [
    (f: ReturnType<typeof fixture>) => { f.connections.get(f.scope.selection.default.id)!.keyExists = false },
    (f: ReturnType<typeof fixture>) => { f.connections.get(f.scope.selection.default.id)!.keysAllowedByCostCenter = false },
    (f: ReturnType<typeof fixture>) => { f.connections.get(f.scope.selection.default.id)!.appliedMethods.keysEnabled = false },
    (f: ReturnType<typeof fixture>) => { f.state.publication.appliedAccess!.grants[0].keysAllowed = false },
  ]) {
    const f = fixture(true)
    approve(f.targets)
    change(f)
    assert.throws(f.validate, /existing key|access methods/)
  }
})

test('budget target and shaped rollup never permit existing default, General, organization or foreign code', () => {
  const f = fixture(true)
  assertModelBudgetTarget(f.scope)
  assert.throws(() => assertModelBudgetTarget(f.scope, f.scope.selection.default.costCenterId), /Budget target/)
  const view = budget(f.scope, 0.01)
  view.costCenter!.code = 'foreign'
  assert.throws(() => pricedBudget(view, f.scope), /isolated monthly/)
  view.costCenter!.code = f.scope.budget!.grant.code
  view.scope = 'organization'
  assert.throws(() => pricedBudget(view, f.scope), /isolated monthly/)
  f.scope.budget!.grant.costCenterId = f.scope.selection.default.costCenterId
  assert.throws(() => assertModelBudgetTarget(f.scope), /Budget target/)
  assert.throws(() => pricedBudget(view, f.scope), /Budget target/)
})

test('missing permission reads no fixtures/profiles, approval lost during reads stops actions permanently', async () => {
  const f = fixture(true)
  let reads = 0
  const forbidden = modelActionGuard(f.targets, 'R10', {}, async () => { reads++ })
  await assert.rejects(forbidden(), /approval/)
  assert.equal(reads, 0)
  const env = approve(f.targets)
  const lost = modelActionGuard(f.targets, 'R10', env, async () => { reads++; f.scope.approval!.expiresAt = '2000-01-01T00:00:00Z' })
  await assert.rejects(lost(), /expired/)
  approve(f.targets)
  await assert.rejects(lost(), /guard stopped/)
  assert.equal(reads, 1)
})

test('current default/center mismatch prevents the next send and shared write without retries or repaired-state resumption', async () => {
  const f = fixture(true)
  const env = approve(f.targets)
  let reads = 0
  const guard = modelActionGuard(f.targets, 'R10', env, async () => { reads++; f.validate() })
  let sends = 0
  const call = boundedModelCaller(f.scope, 'https://gateway.invalid', f.connections, new Map([[f.scope.selection.default.persona, 'offline-token']]),
    async () => { sends++; return success() }, new ModelAllowance(f.scope), () => {}, guard)
  const g = f.scope.selection.default
  await call(g, g.code, 'baseline', 200)
  f.profiles.get(g.persona)!.defaultCostCenter!.id = 'changed'
  await assert.rejects(call(g, g.code, 'drift', 200), /default flag/)
  f.profiles.get(g.persona)!.defaultCostCenter!.id = g.costCenterId
  await assert.rejects(call(g, g.code, 'retry', 200), /guard stopped/)
  let writes = 0
  const save = async () => { await guard(); writes++ }
  await assert.rejects(save(), /guard stopped/)
  assert.equal(sends, 1)
  assert.equal(writes, 0)
  assert.equal(reads, 2)
})

test('fixture reader only uses approved GET readers, reads each profile/center once, and makes no mutations', async () => {
  const f = fixture(true)
  const paths: string[] = []
  const responses = new Map<string, unknown>([
    [`/api/v1/publications/${f.scope.publicationId}`, f.state.publication],
    [`/api/v1/entitlements?resource=${f.scope.modelApiId}`, f.state.grants],
  ])
  for (const [id, center] of f.centers) responses.set(`/api/v1/cost-centers/${id}`, center)
  for (const [id, c] of f.connections) responses.set(`/api/v1/me/entitlements/${id}/connection`, c)
  const reader = (holder?: string) => ({
    get: async <T>(path: string): Promise<T> => {
      paths.push(`${holder ?? 'admin'}:${path}`)
      const value = path === '/api/v1/portal/me' && holder ? f.profiles.get(holder) : responses.get(path)
      assert.notEqual(value, undefined, 'No foreign route or write is supported')
      return structuredClone(value) as T
    },
  })
  const holders = new Map([...f.profiles.keys()].map((p) => [p, reader(p)]))
  const before = structuredClone(f.state)
  const result = await readModelFixtureState(f.scope, reader(), holders)
  assertModelFixtureState(f.targets, f.scope, result, f.identities)
  assert.equal(paths.filter((p) => p.endsWith('/api/v1/portal/me')).length, 2)
  assert.equal(paths.filter((p) => p.includes('/api/v1/cost-centers/')).length, 4)
  assert.equal(paths.filter((p) => p.endsWith('/connection')).length, 5)
  assert.deepEqual(f.state, before)
})

test('the actual live spec skips before manifest/profile access without scope, and before profiles without approval', () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'mosaic-model-no-permission-'))
  const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('MOSAIC_E2E_')))
  try {
    for (const scope of ['', modelTargets().modelJourneys!.ownerTag]) {
      const result = spawnSync(process.execPath, [join(e2eRoot, 'node_modules', '@playwright', 'test', 'cli.js'),
        'test', '55-model-budgets', '--reporter=line', '--output', join(stateDir, 'results')], {
        cwd: e2eRoot, encoding: 'utf8', timeout: 60_000,
        env: { ...cleanEnv, MOSAIC_E2E_MODEL_SCOPE: scope, MOSAIC_E2E_STATE_DIR: stateDir,
          MOSAIC_E2E_TARGETS: join(e2eRoot, scope ? 'targets.example.json' : 'targets.nonexistent-offline-proof.json') },
      })
      assert.equal(result.status, 0, result.stdout + result.stderr)
      assert.match(result.stdout, /5 skipped/)
      assert.ok(readdirSync(stateDir).every((name) => name === 'results'), 'No persona profile or live-driver state may be created')
    }
    const plan = spawnSync(process.execPath, [join(e2eRoot, 'tools', 'model-plan.ts')], {
      cwd: e2eRoot, encoding: 'utf8', timeout: 10_000,
      env: { ...cleanEnv, MOSAIC_E2E_STATE_DIR: stateDir, MOSAIC_E2E_TARGETS: join(e2eRoot, 'targets.example.json') },
    })
    assert.equal(plan.status, 0, plan.stderr)
    assert.match(plan.stdout, /Missing, expired or changed exact-scope owner approval/)
    assert.ok(readdirSync(stateDir).every((name) => name === 'results'))
  } finally {
    rmSync(stateDir, { recursive: true, force: true })
  }
})
