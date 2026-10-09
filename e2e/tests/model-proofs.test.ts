import assert from 'node:assert/strict'
import { test } from 'node:test'
import { budgetGateProblems, budgetProof, pricedBudget } from '../src/model-budget.ts'
import { cents, modelGrants, modelReadiness, modelScopeHash, parseModelJourneys } from '../src/model-config.ts'
import { ModelAllowance, budgetPrompt, boundedModelCaller, connectionProblems, modelRoute, modelUsage, pooledTokenProof, selectionProof, type ModelReply } from '../src/model-runtime.ts'
import { runtimeTokenProblems } from '../src/model-live.ts'
import { budget, connection, modelScope, modelTargets, observation, success } from './model-fixtures.ts'

test('dedicated manifest and approval are absent by default and generic flags authorize nothing', () => {
  const targets = modelTargets()
  const scope = targets.modelJourneys!
  scope.price.date = new Date().toISOString().slice(0, 10)
  assert.ok(modelReadiness(targets, 'R10', { MOSAIC_E2E_ALLOW_WRITES: '1', MOSAIC_E2E_SEND_MODEL_REQUESTS: '1' }).length >= 2)
  scope.approval = { reference: 'offline-test-only', expiresAt: new Date(Date.now() + 3600_000).toISOString(), scopeSha256: modelScopeHash(targets),
    journeys: ['R10', 'R11', 'R12', 'R14'], revealExistingKey: true, budgetWritesAcrossAllManagedGateways: true }
  const env = { MOSAIC_E2E_MODEL_SCOPE: scope.ownerTag, MOSAIC_E2E_MODEL_JOURNEY: 'R10' }
  assert.deepEqual(modelReadiness(targets, 'R10', env), [])
  scope.bounds.maxRequests -= 1
  assert.match(modelReadiness(targets, 'R10', env).join(), /changed exact-scope/)
  scope.approval.scopeSha256 = modelScopeHash(targets)
  scope.approval.budgetWritesAcrossAllManagedGateways = false
  assert.match(modelReadiness(targets, 'R12', { ...env, MOSAIC_E2E_MODEL_JOURNEY: 'R12' }).join(), /ALL managed gateways/)
  scope.approval.expiresAt = '2000-01-01T00:00:00Z'
  assert.ok(modelReadiness(targets, 'R11', env).length)
})

test('rejects General, duplicate grants/callers, secret fields and unbounded paid scope', () => {
  const targets = modelTargets()
  for (const change of [
    (s: ReturnType<typeof modelScope>) => { s.selection.default.code = 'general' },
    (s: ReturnType<typeof modelScope>) => { s.pool.second.id = s.pool.first.id },
    (s: ReturnType<typeof modelScope>) => { s.pool.second.persona = s.pool.first.persona },
    (s: ReturnType<typeof modelScope>) => { s.bounds.maxRequests = 81 },
    (s: ReturnType<typeof modelScope>) => { s.price.inputPerMillion = 0 },
    (s: ReturnType<typeof modelScope>) => { s.budget!.raisedAmount = s.budget!.amount },
  ]) {
    const scope = modelScope()
    change(scope)
    assert.throws(() => parseModelJourneys(scope, targets.personas))
  }
  assert.throws(() => parseModelJourneys({ ...modelScope(), key: 'never-a-secret' }, targets.personas), /unknown field/)
})

test('connection safeguards and route reject pending/foreign/alias-shaped or other-origin data', () => {
  const scope = modelScope()
  const g = scope.selection.default
  const c = connection(g, scope)
  assert.deepEqual(connectionProblems(c, g, scope), [])
  assert.match(modelRoute(c, 'https://gateway.invalid'), /deployment-alias\/chat\/completions/)
  assert.throws(() => modelRoute(c, 'https://other.invalid'))
  c.runtime.status = 'pending'
  c.costCenter.id = 'general'
  c.grantLimits.tokens!.tokenQuota = 10
  assert.equal(connectionProblems(c, g, scope).length, 3)
})

test('dedicated model token must have the holder, tenant, runtime audience, Models.Invoke and whole-run lifetime', () => {
  const targets = modelTargets()
  const scope = targets.modelJourneys!
  const g = scope.selection.default
  const c = connection(g)
  const claims = { preferred_username: targets.personas[g.persona].upn, tid: targets.tenantId, aud: c.entraAudience,
    scp: 'Models.Invoke', exp: Math.floor(Date.now() / 1000) + 7200 }
  const jwt = (value: object) => `header.${Buffer.from(JSON.stringify(value)).toString('base64url')}.signature`
  assert.deepEqual(runtimeTokenProblems(jwt(claims), targets, g.persona, c), [])
  for (const change of [{ scp: 'Mcp.Invoke' }, { exp: 1 }, { aud: targets.origins.api }, { tid: 'foreign' }, { preferred_username: 'different@example.invalid' }]) {
    assert.ok(runtimeTokenProblems(jwt({ ...claims, ...change }), targets, g.persona, c).length)
  }
  assert.ok(runtimeTokenProblems(undefined, targets, g.persona, c).length)
})

test('200 alone, unknown usage and oversized usage are not a real successful model proof', async () => {
  assert.throws(() => modelUsage({ success: true }))
  assert.throws(() => modelUsage({ ...success().body as object, usage: { prompt_tokens: 12, completion_tokens: 2, total_tokens: 999 } }))
  const scope = modelScope()
  const g = scope.selection.default
  let sends = 0
  const allowance = new ModelAllowance(scope)
  const call = boundedModelCaller(scope, 'https://gateway.invalid', new Map([[g.id, connection(g)]]), new Map([[g.persona, 'offline-credential']]), async () => {
    sends++
    return { status: 200, headers: {}, body: { choices: [{ message: { content: 'OK' } }] } }
  }, allowance, () => {})
  await assert.rejects(call(g, g.code, 'missing-usage', 200))
  await assert.rejects(call(g, g.code, 'retry', 200), /BEFORE sending/)
  assert.equal(sends, 1)
})

test('every request including expected denials reserves spend BEFORE sending; transport failure stops retries', async () => {
  const scope = modelScope()
  scope.bounds.maxUsd = 0.000001
  const g = scope.selection.default
  let sends = 0
  const create = (reply: () => Promise<ModelReply>) => boundedModelCaller(scope, 'https://gateway.invalid', new Map([[g.id, connection(g)]]),
    new Map([[g.persona, 'offline-credential']]), reply, new ModelAllowance(scope), () => {})
  await assert.rejects(create(async () => { sends++; return success() })(g, 'invalid,code', 'denial', 403), /BEFORE sending/)
  assert.equal(sends, 0)
  scope.bounds.maxUsd = 0.02
  const call = create(async () => { sends++; throw new Error('transport may contain credentials') })
  await assert.rejects(call(g, g.code, 'transport', 200), /usage is unknown/)
  await assert.rejects(call(g, g.code, 'retry', 200), /BEFORE sending/)
  assert.equal(sends, 1)
  scope.approval = { reference: 'offline-expired', expiresAt: '2000-01-01T00:00:00Z', scopeSha256: '0'.repeat(64),
    journeys: ['R11'], revealExistingKey: false, budgetWritesAcrossAllManagedGateways: false }
  assert.throws(() => new ModelAllowance(scope).reserve(), /BEFORE sending/)
  scope.price.cachedInputPerMillion = NaN
  assert.throws(() => new ModelAllowance(scope), /Unknown cached price/)
})

test('R10 runs exact token and key matrix, and fails an incorrectly allowed unknown code', async () => {
  const scope = modelScope()
  const expected: { code?: string; key?: string; status: number }[] = []
  await selectionProof(scope, async (g, code, label, status, key) => {
    expected.push({ code, key, status: status as number })
    return observation(g, label, status as number)
  }, 'offline-key')
  assert.equal(expected.length, 8)
  assert.deepEqual(expected.map((e) => e.status), [200, 200, 200, 403, 403, 403, 200, 200])
  assert.equal(expected[2].code, undefined)
  assert.equal(expected[5].key, 'offline-key')
  const grants = modelGrants(scope)
  const call = boundedModelCaller(scope, 'https://gateway.invalid', new Map(grants.map((g) => [g.id, connection(g)])),
    new Map(grants.map((g) => [g.persona, 'offline-token'])), async () => success(), new ModelAllowance(scope), () => {})
  await assert.rejects(selectionProof(scope, call, 'offline-key'), /entra-unknown: expected 403, received 200/)
})

test('R11 proves a shared MODEL monthly counter and independent own quotas; wrong per-grant pool, missing headers and upstream 429 fail', async () => {
  async function run(wrong: 'per-grant' | 'missing' | 'upstream' | undefined) {
    const scope = modelScope()
    const grants = modelGrants(scope)
    const own = new Map<string, number>()
    let spent = 0
    const call = boundedModelCaller(scope, 'https://gateway.invalid', new Map(grants.map((g) => [g.id, connection(g)])),
      new Map(grants.map((g) => [g.persona, `offline-${g.persona}`])), async (_url, headers) => {
        const id = headers.Authorization.endsWith('guest') ? scope.pool.second.id : scope.pool.first.id
        if (headers['x-mosaic-cost-center'] === scope.selection.other.code) return success()
        const used = own.get(id) ?? 0
        const deny = spent >= scope.pool.monthlyTokens - 12
        if (!deny) { spent += 14; own.set(id, used + 14) }
        const reply = deny ? { status: 429, headers: {}, body: { error: { code: 'RateLimitExceeded' } } } : success()
        if (wrong !== 'missing') Object.assign(reply.headers, {
          'x-mosaic-cost-center-remaining-quota-tokens': String(Math.max(0, scope.pool.monthlyTokens - (wrong === 'per-grant' ? (own.get(id) ?? 0) : spent))),
          'x-mosaic-remaining-tokens': wrong === 'upstream' && deny ? '0' : '1900',
          'x-mosaic-remaining-quota-tokens': String(1000 - (own.get(id) ?? 0)),
        })
        return reply
      }, new ModelAllowance(scope), () => {})
    return pooledTokenProof(scope, new Map(grants.map((g) => [g.id, connection(g)])), call, async () => {})
  }
  const proof = await run(undefined)
  assert.equal(proof.at(-1)?.case, 'pool-other-cost-center')
  assert.ok(proof.some((e) => e.grant === modelScope().pool.second.id && e.status === 200))
  for (const wrong of ['per-grant', 'missing', 'upstream'] as const) await assert.rejects(run(wrong))
})

test('budget minimum/thresholds compare rounded cents including one-cent simultaneous 80/100', () => {
  assert.equal(cents(0.009), 1)
  assert.equal(cents(0.004), 0)
  assert.equal(cents(0.005), 0)
  assert.equal(cents(0.015), 2)
  const scope = modelScope()
  assert.equal(pricedBudget(budget(scope, 0.009), scope), 1)
  const wrong = budget(scope, 0.01)
  wrong.status.unpricedTokens = 1
  assert.throws(() => pricedBudget(wrong, scope))
  wrong.status.unpricedTokens = 0
  wrong.status.monthToDate = null
  assert.throws(() => pricedBudget(wrong, scope))
})

test('R12/R14 round trip uses actual shaped priced state and all gateways; wrong spend, sends and partial gates fail', async () => {
  const scope = modelScope()
  const state = budget(scope, 0.01)
  assert.deepEqual(budgetGateProblems(state, scope, false), [])
  state.status.gateways.pop()
  assert.ok(budgetGateProblems(state, scope, false).length)
  const run = async (journey: 'R12' | 'R14', initial: number, wrong = false) => {
    let phase = 0
    const saves: number[] = []
    return budgetProof(scope, journey, {
      read: async () => {
        const view = budget(scope, phase === 0 ? initial : 0.01, phase === 1)
        if (phase === 0) view.status.through = new Date(Date.now() - 1000).toISOString()
        if (wrong) view.recipients = ['not-authorized@example.invalid']
        return view
      },
      save: async (amount) => { saves.push(amount); phase++; return { startedAt: new Date().toISOString(), savedAt: new Date().toISOString() } },
      call: async (g, _code, label, status) => observation(g, label, status === 'budget' || status === 'budget-spend' ? (phase === 1 ? 403 : 200) : status as number),
      banner: async () => {}, pause: async () => {},
    })
  }
  assert.equal((await run('R12', 0.01)).first403.status, 403)
  assert.equal((await run('R14', 0)).firstSuccess.status, 200)
  await assert.rejects(run('R14', 0.01), /start below/)
  await assert.rejects(run('R12', 0), /real priced spend/)
  await assert.rejects(run('R14', 0, true), /recipients/)
})

test('R14 executes bounded spend then small probes, waits for independent advancing priced state, selective 403/banner and raise', async () => {
  const scope = modelScope()
  scope.price.inputPerMillion = 2
  const grants = modelGrants(scope)
  const fixture = scope.budget!
  let phase = 0
  let spend = 0
  let reported = 0
  let blocked = false
  let pendingTicks = 0
  let longCalls = 0
  let smallAfterThreshold = 0
  const banners: boolean[] = []
  const allowance = new ModelAllowance(scope)
  const observations: import('../src/model-runtime.ts').ModelObservation[] = []
  const call = boundedModelCaller(scope, 'https://gateway.invalid', new Map(grants.map((g) => [g.id, connection(g)])),
    new Map(grants.map((g) => [g.persona, `offline-${g.persona}`])), async (_url, headers, payload) => {
      const message = (payload as { messages: { content: string }[] }).messages[0].content
      if (blocked && headers['x-mosaic-cost-center'] === fixture.grant.code) {
        return { status: 403, headers: {}, body: `Access denied. Cost center ${fixture.grant.code} has used its monthly budget, so its calls are refused.` }
      }
      const long = message === budgetPrompt
      if (long) longCalls++
      if (!long && cents(spend) >= 1) smallAfterThreshold++
      const reply = success()
      if (long) reply.body = { choices: [{ message: { content: 'OK' } }], usage: {
        prompt_tokens: 400, completion_tokens: 2, total_tokens: 402, prompt_tokens_details: { cached_tokens: 0 },
      } }
      if (headers['x-mosaic-cost-center'] === fixture.grant.code) spend += ((long ? 400 : 12) * 2 + 2 * 2) / 1_000_000
      return reply
    }, allowance, (o) => observations.push(o))
  const result = await budgetProof(scope, 'R14', {
    read: async () => {
      const view = budget(scope, reported, blocked)
      view.amount = phase === 1 ? fixture.amount : fixture.raisedAmount
      view.status.through = new Date(Date.now() - (phase === 0 ? 2000 : 1000)).toISOString()
      return view
    },
    save: async (amount) => {
      assert.equal(amount, phase === 0 ? fixture.amount : fixture.raisedAmount)
      phase++
      if (phase === 2) blocked = false
      return { startedAt: new Date().toISOString(), savedAt: new Date().toISOString() }
    },
    call,
    pause: async () => {
      if (cents(spend) >= 1 && ++pendingTicks >= 2) { reported = spend; blocked = phase === 1 }
    },
    banner: async (value) => { banners.push(value) },
  })
  assert.equal(result.first403.status, 403)
  assert.equal(result.firstSuccess.status, 200)
  assert.ok(longCalls > 1 && smallAfterThreshold > 1)
  assert.deepEqual(banners, [true, false])
  assert.ok(observations.some((o) => o.case === 'budget-other-cost-center' && o.status === 200))
  assert.ok(allowance.summary().reservedUsd <= scope.bounds.maxUsd)
  assert.ok(allowance.summary().reservedPromptTokens <= scope.bounds.maxPromptTokens)
})
